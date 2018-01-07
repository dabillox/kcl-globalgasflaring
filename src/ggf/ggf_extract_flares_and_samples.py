#!/home/users/dnfisher/soft/virtual_envs/ggf/bin/python2

import os
import sys
import logging
from datetime import datetime

import epr
import numpy as np
import pandas as pd

import src.config.constants as proc_const
import src.models.atsr_pixel_size as atsr_pixel_size
import src.config.filepaths as fp

import matplotlib.pyplot as plt


def read_atsr(path_to_ats_data):
    return epr.Product(path_to_ats_data)


def define_sensor(path_to_data):
    if 'N1' in path_to_data:
        sensor = 'ats'
    if 'E2' in path_to_data:
        sensor = 'at2'
    if 'E1' in path_to_data:
        sensor = 'at1'
    return sensor


def make_night_mask(ats_product):
    solar_elev_angle = np.deg2rad(ats_product.get_band('sun_elev_nadir').read_as_array())
    solar_zenith_angle = np.rad2deg(np.arccos(np.sin(solar_elev_angle)))
    return solar_zenith_angle >= proc_const.day_night_angle


def detect_hotspots(ats_product):
    swir = ats_product.get_band('reflec_nadir_1600').read_as_array()
    nan_mask = np.isnan(swir)  # get rid of SWIR nans also
    return (swir > proc_const.swir_thresh) & ~nan_mask


def make_cloud_mask(ats_product):
    cloud_mask = ats_product.get_band('cloud_flags_nadir').read_as_array()
    # over land or water and cloud free (i.e. bit 0 is set (cloud free land)  or unset(cloud free water))
    return cloud_mask <= 1


def get_arcmin(x):
    '''
    rounds the data decimal fraction of a degree
    to the nearest arc minute
    '''
    neg_values = x < 0

    abs_x = np.abs(x)
    floor_x = np.floor(abs_x)
    decile = abs_x - floor_x
    minute = np.around(decile * 60)  # round to nearest arcmin
    minute_fraction = minute * 0.01  # convert to fractional value (ranges from 0 to 0.6)

    max_minute = minute_fraction > 0.59

    floor_x[neg_values] *= -1
    floor_x[neg_values] -= minute_fraction[neg_values]
    floor_x[~neg_values] += minute_fraction[~neg_values]

    # deal with edge cases, and just round them all up
    if np.sum(max_minute) > 0:
        floor_x[max_minute] = np.around(floor_x[max_minute])

    # now to get rid of rounding errors and allow comparison multiply by 100 and convert to int
    floor_x = (floor_x * 100).astype(int)

    return floor_x


def myround(x, dec=20, base=60. / 3600):
    return np.round(base * np.round(x / base), dec)


def radiance_from_reflectance(pixels, ats_product, sensor):
    # convert from reflectance to radiance see Smith and Cox 2013
    return pixels / 100.0 * proc_const.solar_irradiance[sensor] * sun_earth_distance(ats_product.id_string) ** 2 / np.pi


def radiance_from_BT(wvl, temp):
    '''
    wvl: wavelngth (microns)
    temp: temperature (kelvin)
    '''
    c1 = 1.19e-16  # W m-2 sr-1
    c2 = 1.44e-2  # mK
    wt = (wvl * 1.e-6) * temp  # m K
    d = (wvl * 1.e-6) ** 5 * (np.exp(c2 / wt) - 1)
    return c1 / d * 1.e-6  # W m-2 sr-1 um-1


def sun_earth_distance(id_string):
    doy = datetime.strptime(id_string[14:22], "%Y%m%d").timetuple().tm_yday
    return 1 + 0.01672 * np.sin(2 * np.pi * (doy - 93.5) / 365.0)


def compute_pixel_size(samples):
    pix_sizes = atsr_pixel_size.compute() * 1000000  # convert from km^2 to m^2
    return pix_sizes[samples]


def compute_frp(pixel_radiances, pixel_sizes, sensor):
    return pixel_sizes * proc_const.frp_coeff[sensor] * pixel_radiances / 1000000  # in MW


def construct_hotspot_line_sample_df(product, hotspot_mask):
    lines, samples = np.where(hotspot_mask)
    lats = product.get_band('latitude').read_as_array()[hotspot_mask]
    lons = product.get_band('longitude').read_as_array()[hotspot_mask]

    # round geographic data to desired reoslution
    lats_arcmin = get_arcmin(lats)
    lons_arcmin = get_arcmin(lons)

    df = pd.DataFrame()
    datasets = [lats_arcmin, lons_arcmin, lines, samples]
    names = ['lats_arcmin', 'lons_arcmin', 'lines', 'samples']
    for k, v in zip(names, datasets):
        df[k] = v
    return df


def determine_thermal_background_contribution(flare_line_sample_df, product, hotspot_mask, cloud_free_mask):
    # get MWIR and LWIR data
    mwir_bt = product.get_band('btemp_nadir_0370').read_as_array()

    # bg window rad
    bg_sizes = [2, 4, 6, 8, 10, 12]

    # get a line and sample estimate for each cluster
    cluster_df = flare_line_sample_df.groupby(['lats_arcmin', 'lons_arcmin'], as_index=False).agg({'lines': np.max,
                                                                                                   'samples': np.max})
    # get the background radiances
    mwir_bg = []
    cloud_bg_pc = []
    hotspot_bg_pc = []
    inval_pixels_bg_pc = []
    bg_size_used = []
    for i, row in cluster_df.iterrows():
        valid_bg = False
        for bg_size in bg_sizes:

            # check for edges
            min_x = row.samples - bg_size if row.samples - bg_size > 0 else 0
            max_x = row.samples + bg_size if row.samples + bg_size < 511 else 511
            min_y = row.lines - bg_size if row.lines - bg_size > 0 else 0
            max_y = row.lines + bg_size if row.lines + bg_size < cloud_free_mask.shape[0] else cloud_free_mask.shape[0]

            # build mask
            bg_mask = cloud_free_mask[min_y:max_y, min_x:max_x] & \
                      ~hotspot_mask[min_y:max_y, min_x:max_x] & \
                      (mwir_bt[min_y:max_y, min_x:max_x] > 0)

            if np.sum(bg_mask) / float(bg_mask.size) > 0.6:
                valid_bg = True
                break

        if valid_bg:
            mwir_subset = mwir_bt[min_y:max_y, min_x:max_x]
            mwir_subset = radiance_from_BT(3.7, mwir_subset)
            mwir_bg.append(np.mean(mwir_subset[bg_mask]))

        else:
            mwir_bg.append(-1)

        # store mask info for determining what is causing fails
        bg_size_used.append(bg_size)
        mask_size = float(bg_mask.size)
        cloud_bg_pc.append(np.sum(~cloud_free_mask[min_y:max_y, min_x:max_x])/mask_size)
        hotspot_bg_pc.append(np.sum(hotspot_mask[min_y:max_y, min_x:max_x])/mask_size)
        inval = (mwir_bt[min_y:max_y, min_x:max_x] <= 0)
        inval_pixels_bg_pc.append(np.sum(inval)/mask_size)


    cluster_df['mwir_bg'] = mwir_bg
    cluster_df['cloud_bg_pc'] = cloud_bg_pc
    cluster_df['hotspot_bg_pc'] = hotspot_bg_pc
    cluster_df['inval_pixels_bg_pc'] = inval_pixels_bg_pc
    cluster_df['bg_size_used'] = bg_size_used

    cluster_df.drop(['lines', 'samples'], axis=1, inplace=True)

    # return the backgorunds
    return cluster_df


def construct_hotspot_df(flare_df, hotspot_mask, cloud_free_mask,
                         product, resolution, sensor):
    coords = [flare_df.lines.values, flare_df.samples.values]

    lats = product.get_band('latitude').read_as_array()[coords]
    lons = product.get_band('longitude').read_as_array()[coords]

    # round geographic data to desired reoslution
    rounded_lats = myround(lats, base=resolution)
    rounded_lons = myround(lons, base=resolution)

    swir_reflectances = product.get_band('reflec_nadir_1600').read_as_array()[coords]
    swir_radiances = radiance_from_reflectance(swir_reflectances, product, sensor)

    mwir_bt = product.get_band('btemp_nadir_0370').read_as_array()[coords]
    mwir_radiances = radiance_from_BT(3.7, mwir_bt)
    mwir_radiances[mwir_radiances < 0] = np.nan

    pixel_size = compute_pixel_size(flare_df.samples.values)
    frp = compute_frp(swir_radiances, pixel_size, sensor)

    solar_elev_angle = product.get_band('sun_elev_nadir').read_as_array()[coords]
    view_elev_angle = product.get_band('view_elev_nadir').read_as_array()[coords]

    datasets = [rounded_lats, rounded_lons, frp,
                swir_radiances, swir_reflectances, mwir_radiances,
                solar_elev_angle, view_elev_angle, pixel_size]
    names = ['lats', 'lons', 'frp',
             'swir_radiances', 'swir_reflectances', 'mwir_radiances',
             'sun_elev', 'view_elev', 'pixel_size']
    for k, v in zip(names, datasets):
        flare_df[k] = v

    # get the background for each cluster
    thermal_bg_df = determine_thermal_background_contribution(flare_df, product,
                                                              hotspot_mask, cloud_free_mask)
    flare_df = flare_df.merge(thermal_bg_df, on=['lats_arcmin', 'lons_arcmin'])

    return flare_df


def construct_sample_df(product, resolution, cloud_free_mask, hotspot_mask, sample_mask):
    # assign type mask
    types = np.zeros(cloud_free_mask.shape)
    types[cloud_free_mask] = 2
    types[hotspot_mask] = 1
    types = types[sample_mask]

    lats = product.get_band('latitude').read_as_array()[sample_mask]
    lons = product.get_band('longitude').read_as_array()[sample_mask]

    # round geographic data to desired reoslution
    lats_arcmin = get_arcmin(lats)
    lons_arcmin = get_arcmin(lons)
    rounded_lats = myround(lats, base=resolution)
    rounded_lons = myround(lons, base=resolution)

    df = pd.DataFrame()
    datasets = [rounded_lats, rounded_lons, lats_arcmin, lons_arcmin, types]
    names = ['lats', 'lons', 'lats_arcmin', 'lons_arcmin', 'types']
    for k, v in zip(names, datasets):
        df[k] = v
    return df


def sample_type_aggregator(a):
    if np.in1d(1, a)[0]:  # if a gas flare pixel in grid cell, then return as gas flare type, else cloud
        return 1
    else:
        return 2


def group_sample_df(df):
    return df.groupby(['lats_arcmin', 'lons_arcmin'], as_index=False).agg({'types': sample_type_aggregator,
                                                                           'lats': np.mean,
                                                                           'lons': np.mean})


def hotspot_type_aggregator(a):
    '''
    Just take the most common value.
    0 is non-flare hotspot
    1 is flare hotspot
    2 is non retrieval caused by missing or warm background
    '''
    return np.bincount(a).argmax()


def group_hotspot_df(df):
    agg_dict = {'frp': np.sum,  # sum to get the total FRP in the grid cell
                'swir_radiances': np.mean,
                'mwir_radiances': np.mean,
                'mwir_bg': np.mean,
                'pixel_size': np.sum,
                'lats': np.mean,
                'lons': np.mean,
                'cloud_bg_pc': np.mean,
                'hotspot_bg_pc': np.mean,
                'inval_pixels_bg_pc': np.mean,
                'bg_size_used': np.mean,
                }
    return df.groupby(['lats_arcmin', 'lons_arcmin'], as_index=False).agg(agg_dict)


def extend_df(df, sensor, id_string, hotspot_df=False):
    df['year'] = int(id_string[14:18])
    df['month'] = int(id_string[18:20])
    df['day'] = int(id_string[20:22])
    df['hhmm'] = int(id_string[23:27])
    df['sensor'] = sensor

    if hotspot_df:
        df['se_dist'] = sun_earth_distance(id_string)
        df['frp_coeff'] = proc_const.frp_coeff[sensor]


def main():
    # some processing constants
    resolution = 60 / 3600.  # Degrees. same as with monthly aggregation

    # load in the persistent flare location dataframe
    flare_df = pd.read_csv(os.path.join(fp.path_to_cems_output_l3, 'all_sensors', 'all_flare_locations.csv'))

    # read in the atsr product
    path_to_data = sys.argv[1]
    path_to_output = sys.argv[2]

    atsr_data = read_atsr(path_to_data)
    sensor = define_sensor(path_to_data)

    # set up various data masks
    night_mask = make_night_mask(atsr_data)
    cloud_free_mask = make_cloud_mask(atsr_data)

    potential_hotspot_mask = detect_hotspots(atsr_data)
    potential_sample_mask = cloud_free_mask | potential_hotspot_mask  # cloud free or high SWIR

    hotspot_mask = night_mask & potential_hotspot_mask
    sample_mask = night_mask & potential_sample_mask

    # do the processing for flares
    hotspot_line_sample_df = construct_hotspot_line_sample_df(atsr_data, hotspot_mask)
    flare_line_sample_df = pd.merge(flare_df, hotspot_line_sample_df, on=['lats_arcmin', 'lons_arcmin'])
    flare_hotspot_df = construct_hotspot_df(flare_line_sample_df, hotspot_mask, cloud_free_mask,
                                            atsr_data, resolution, sensor)
    grouped_hotspot_df = group_hotspot_df(flare_hotspot_df)
    extend_df(grouped_hotspot_df, sensor, atsr_data.id_string, hotspot_df=True)
    flare_output_fname = atsr_data.id_string.split('.')[0] + '_flares.csv'
    flare_output_fname = flare_output_fname.replace(flare_output_fname[0:3], sensor.upper())
    flare_csv_path = os.path.join(path_to_output, flare_output_fname)
    flare_hotspot_df.to_csv(flare_csv_path, index=False)

    # do the processing for samples
    sample_df = construct_sample_df(atsr_data, resolution, cloud_free_mask, hotspot_mask, sample_mask)
    grouped_sample_df = group_sample_df(sample_df)
    extend_df(grouped_sample_df, sensor, atsr_data.id_string)
    flare_sample_df = pd.merge(flare_df, grouped_sample_df, on=['lats_arcmin', 'lons_arcmin'])
    sample_output_fname = atsr_data.id_string.split('.')[0] + '_sampling.csv'
    sample_output_fname = sample_output_fname.replace(sample_output_fname[0:3], sensor.upper())
    sample_csv_path = os.path.join(path_to_output, sample_output_fname)
    flare_sample_df.to_csv(sample_csv_path, index=False)


if __name__ == "__main__":
    log_fmt = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    logging.basicConfig(level=logging.INFO, format=log_fmt)
    logger = logging.getLogger(__name__)
    main()
