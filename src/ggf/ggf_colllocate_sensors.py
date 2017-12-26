#!/home/users/dnfisher/soft/virtual_envs/ggf/bin/python2

"""
code description:

"""

import os
import sys
import logging
from datetime import datetime
import glob

import epr
import numpy as np
import scipy.spatial as spatial
import pandas as pd

import src.config.constants as proc_const
import src.config.filepaths as fp


def get_at2_path(ats_path):
    if 'segregated' in ats_path:
    	at2_path, ats_fname = ats_path.split('segregated/')
    else:
        ats_fname = ats_path.split('/')[-1]
        at2_path = ats_path.split(ats_fname)[0]
    at2_path = at2_path.replace('aatsr-v3', 'atsr2-v3')
    at2_path = at2_path.replace('ats_toa_1p', 'at2_toa_1p')
    ats_timestamp = ats_fname[14:25]
    logger.info(at2_path)
    logger.info(ats_timestamp)
    return glob.glob(at2_path + '*' + ats_timestamp + '*.E2')[0]



def read_atsr(path_to_ats_data):
    return epr.Product(path_to_ats_data)


def make_night_mask(ats_product):
    solar_elev_angle = np.deg2rad(ats_product.get_band('sun_elev_nadir').read_as_array())
    solar_zenith_angle = np.rad2deg(np.arccos(np.sin(solar_elev_angle)))
    return solar_zenith_angle >= proc_const.day_night_angle


def detect_flares(ats_product, mask):
    swir = ats_product.get_band('reflec_nadir_1600').read_as_array()
    nan_mask = np.isnan(swir)  # get rid of SWIR nans also
    return (swir > proc_const.swir_thresh) & mask & ~nan_mask


def myround(x, dec=20, base=60. / 3600):
    return np.round(base * np.round(x/base), dec)


def setup_data(ats_product, mask):

    # get reflectances
    reflectances = ats_product.get_band('reflec_nadir_1600').read_as_array()[mask]

    # mask lats and lons
    lats = ats_product.get_band('latitude').read_as_array()[mask]
    lons = ats_product.get_band('longitude').read_as_array()[mask]

    # then round them
    rounded_lats = myround(lats)
    rounded_lons = myround(lons)

    # set up dataframe to group the data
    df = pd.DataFrame({'lats': rounded_lats,
                       'lons': rounded_lons,
                       'reflectances': reflectances})

    # here we can calculate if it is a cloud free or flaring observation using pandas
    grouped = df.groupby(['lats', 'lons'], as_index=False).agg({'reflectances': np.mean})

    # # join together the round coordinates
    # combined_coords = zip(rounded_lats, rounded_lons)
    #
    # # then get the unique lat and lon combinations
    # unique_coords = set(combined_coords)
    #
    # # return them unzipped
    # rounded_lats, rounded_lons = zip(*unique_coords)

    rounded_lats = grouped['lats'].values
    rounded_lons = grouped['lons'].values
    mean_reflectances = grouped['reflectances'].values

    return rounded_lats, rounded_lons, mean_reflectances


def get_flaring_for_orbit(ds, resolution):

    # set up masks that define potential flaring sites
    night_mask = make_night_mask(ds)
    flare_mask = detect_flares(ds, night_mask)

    # get the rounded lats and lons of the potential flaring sites
    rounded_lats, rounded_lons, reflectances = setup_data(ds, flare_mask)

    # set up the cKDTree for querying flare locations
    combined_lat_lon = np.dstack([rounded_lats, rounded_lons])[0]
    orbit_kdtree = spatial.cKDTree(combined_lat_lon)

    # get atsr orbit time
    year = int(ds.id_string[14:18])
    month = int(ds.id_string[18:20])
    day = int(ds.id_string[20:22])
    orbit_time = datetime(year, month, day)

    # load in the flare dataframe
    flare_df = pd.read_csv(os.path.join(fp.path_to_cems_output_l3, 'all_sensors', 'all_flares.csv'))

    # groupby flare id and get the start and stop time
    flare_df = flare_df.groupby(['flare_id'], as_index=False).agg({'lats': np.mean, 'lons': np.mean,
                                                                   'dt_start': np.min, 'dt_stop': np.max})

    flare_df['dt_start'] = pd.to_datetime(flare_df['dt_start'])
    flare_df['dt_stop'] = pd.to_datetime(flare_df['dt_stop'])

    # now subset down the dataframe by time to only those flares
    # that have been seen burning before AND after this orbit
    flare_df = flare_df[(flare_df.dt_start <= orbit_time) &
                        (flare_df.dt_stop >= orbit_time)]
    if flare_df.empty:
        return

    # set up the flare lats and lons for assessment in kdtree
    flare_lat_lon = np.array(zip(flare_df.lats.values, flare_df.lons.values))

    # compare the flare locations to the potential locations in the orbit
    distances, indexes = orbit_kdtree.query(flare_lat_lon)

    # find the flaring locations in the orbit by distance measure
    valid_distances = distances <= resolution / 2.  # TODO think we can drop the /2 and just do <
    flare_id = flare_df.flare_id[valid_distances].values
    matched_lats = combined_lat_lon[indexes[valid_distances], 0]
    matched_lons = combined_lat_lon[indexes[valid_distances], 1]
    matched_reflectances = reflectances[indexes[valid_distances]]

    # set up output df
    output_df = pd.DataFrame({'flare_id': flare_id,
                              'matched_lats': matched_lats,
                              'matched_lons': matched_lons,
                              'reflectances': matched_reflectances
                              })
    return output_df


def main():

    # some processing constants
    resolution = 60 / 3600.  # Degrees. same as with monthly aggregation

    # read in the aatsr product
    path_to_ats_data = sys.argv[1]
    path_to_output = sys.argv[2]

    path_to_at2_data = get_at2_path(path_to_ats_data)
    logger.info('ats_path: ' + path_to_at2_data)

    ats_data = read_atsr(path_to_ats_data)
    at2_data = read_atsr(path_to_at2_data)

    ats_flare_df = get_flaring_for_orbit(ats_data, resolution)
    at2_flare_df = get_flaring_for_orbit(at2_data, resolution)

    # merge on flare ID so that we keep collocated observations
    merged_df = ats_flare_df.merge(at2_flare_df, on='flare_id')

    # write out the recorded flare id's for this orbit
    output_fname = ats_data.id_string.split('.')[0] + '_collocated.csv'
    output_fname = output_fname.replace(output_fname[0:3], 'ats_at2')
    csv_path = os.path.join(path_to_output, output_fname)
    merged_df.to_csv(csv_path, index=False)

if __name__ == "__main__":
    log_fmt = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    logging.basicConfig(level=logging.INFO, format=log_fmt)
    logger = logging.getLogger(__name__)
    main()
