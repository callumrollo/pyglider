# -*- coding: utf-8 -*-
import datetime
import glob
import itertools
import logging
import sys
from math import floor, fmod
import numpy as np
import os
import re
import datetime
import xarray as xr
import yaml
import pyglider.utils as utils
import pandas as pd
import random
import math
import multiprocessing as mp
import time

_log = logging.getLogger(__name__)


def _outputname(f, outdir):
    fnout = os.path.basename(f)
    fns = fnout.split('.')
    fns = fns[:5]
    fns[4] = '%04d' % int(fns[4])
    fns[1] = '%04d' % int(fns[1])
    fnout = ''
    for ff in fns:
        fnout += ff.lower() + '.'
    filenum = int(fns[4])
    return outdir + fnout + 'nc', filenum


def _needsupdating(ftype, fin, fout):
    if not os.path.isfile(fout):
        return True
    return (os.path.getmtime(fin) >= os.path.getmtime(fout))

def _sort(ds):
    return ds.sortby('time')

def _get_raw_filenames(indir):
    """
    Finds all seaexplorer files in a directory and returns randomized list of paths to these files
    Parameters
    ----------
    indir

    Returns
    -------
    list of all seaexplorer files in directory
    """
    filenames = []
    for ftype in ['gli', 'pld1']:
        for rawsub in ['raw', 'sub']:
            _log.info(f'Reading in raw files matching *{ftype}.{rawsub}*')
            d = indir + f'*.{ftype}.{rawsub}.*'
            filenames.append(glob.glob(d))
    filenames_flat = [item for sublist in filenames for item in sublist]
    # randomize this list so that each core in multiprocessing will get a mix of gli and pld files.
    random.shuffle(filenames_flat)
    return filenames_flat


def _remove_bad_timestamps(ds):
    time_ds = ds.time.values
    time_diff = time_ds[1:] - time_ds[:-1]
    if len(time_diff[time_diff < 0]) == 0:
        return ds
    good_timestep = np.ones(len(time_ds), dtype=bool)
    steps = np.arange(1, len(time_ds))
    bad_timestamps = steps[time_diff < 0]
    for stamp in bad_timestamps:
        last_good = time_ds[stamp - 1]
        next_good = steps[time_ds[1:] > last_good][0]
        good_timestep[stamp:next_good] = False
    ds_out = ds.isel(time=good_timestep)
    _log.info(f'negative timestep(s) of {time_diff[time_diff<0]} s identified. '
              f'Removed {len(ds.time) - len(ds_out.time)} points.')
    return ds_out


def raw_to_rawnc(indir, outdir, deploymentyaml, incremental=True, min_samples_in_file=5, cores=None):
    """
    Convert seaexplorer text files to raw netcdf files.

    Parameters
    ----------
    files : list
        list of raw files for proccesing

    outdir : str
        Directory to write the matching ``*.nc`` files. Recommend ``rawnc``.

    deploymentyaml : str
        YAML text file with deployment information for this glider.

    incremental : bool, optional
        If *True* (default), only netcdf files that are older than the
        binary files are re-parsed.

    min_samples_in_file : int
        Minimum number of samples in a raw file to trigger writing a netcdf file.
        Defaults to 5

    cores: int, optional
        Number of cores to use for multiprocessing. If left as None, will use 80 % of cores on machine.

    Returns
    -------
    status : bool
        *True* success.

    Notes
    -----

    This process can be slow for many files. Try using as many cores as you can spare.

    """
    start = time.time()
    # Create out directory for netcdfs if it does not exist
    try:
        os.mkdir(outdir)
    except FileExistsError:
        pass
    input_files = _get_raw_filenames(indir)
    # Set up for multiprocessing. If number of cores not specified, use 80% of them, rounded down
    if not cores:
        total_cores = mp.cpu_count()
        cores = int(math.floor(0.8 * total_cores))
    chunk_size = int(math.ceil(len(input_files)/cores))
    chunked_list = [input_files[i:i + chunk_size] for i in range(0, len(input_files), chunk_size)]
    processes = []

    for i, chunk in enumerate(chunked_list):
        p = mp.Process(target=_raw_to_rawnc_worker, args=(chunk, outdir),
                       kwargs={'incremental':incremental, 'min_samples_in_file':min_samples_in_file})
        processes.append(p)
        p.start()

    for p in processes:
        p.join()

    outfiles = glob.glob(f'{outdir}*pld1*')
    if outfiles:
        outfile_times = []
        for fout in outfiles:
            outfile_times.append(os.path.getmtime(fout))
        for fout_time in outfile_times:
            if fout_time > start:
                _log.info('All raw files converted to nc')
                return True
    _log.warning('No valid, unprocessed raw files found')
    return False


def _raw_to_rawnc_worker(files, outdir, incremental=True, min_samples_in_file=5):
    fnum = np.zeros(len(files))
    # these files don't sort properly, but we can sort them here.
    for n, f in enumerate(files):
        p = os.path.basename(f).split('.')
        fnum[n] = p[4]
    inds = np.argsort(fnum)
    files = [files[ind] for ind in inds]
    _log.info(f'found {files}')
    badfiles = []
    for ind, f in enumerate(files):
        if 'pld1' in f:
            ftype = 'pld1'
        else:
            ftype = 'gli'
        # output name:
        fnout, filenum = _outputname(f, outdir)
        if not incremental or _needsupdating(ftype, f, fnout):
            _log.info(f'Doing: {f} to {fnout}')
            out = pd.read_csv(f, header=0, delimiter=';',
                                    parse_dates=True, index_col=0,
                                    dayfirst=True)
            # If AD2CP data present, convert the timestamps to datetime type
            if 'AD2CP_TIME' in out.columns:
                out['AD2CP_TIME'] = pd.to_datetime(out.AD2CP_TIME)
            with out.to_xarray() as outx:

                key = list(outx.coords.keys())[0]
                outx = outx.rename({key:'time'})
                # dumb time down to seconds since 1970-01-01
                outx['time'] = outx['time'].astype(np.float64)/1e9
                outx['time'].attrs['units'] = (
                    'seconds since 1970-01-01T00:00:00Z')
                outx['fnum'] = ('time',
                    int(filenum) * np.ones(len(outx['time'])))
                if ftype == 'gli':
                    outx = _remove_bad_timestamps(outx)
                    outx.to_netcdf(fnout[:-3]+'.nc', 'w')
                else:
                    if outx.indexes["time"].size > min_samples_in_file:
                        outx = _remove_bad_timestamps(outx)
                        outx.to_netcdf(f'{fnout[:-3]}.nc', 'w',
                                       unlimited_dims=['time'])
                    else:
                        _log.warning(f'Number of sensor data points too small. Skipping nc write')
                        badfiles.append(f)
    if len(badfiles) > 0:
        _log.warning('Some files could not be parsed:')
        for fn in badfiles:
            _log.warning('%s', fn)
    return True


def merge_rawnc(indir, outdir, deploymentyaml, incremental=False, kind='raw'):
    """
    Merge all the raw netcdf files in indir.  These are meant to be
    the raw flight and science files from the slocum.

    Parameters
    ----------
    indir : str
        Directory where the raw ``*.ebd.nc`` and ``*.dbd.nc`` files are.
        Recommend: ``./rawnc``

    outdir : str
        Directory where merged raw netcdf files will be put. Recommend:
        ``./rawnc/``.  Note that the netcdf files will be named following
        the data in *deploymentyaml*:
        ``glider_nameglider_serial-YYYYmmddTHHMM-rawebd.nc`` and
        ``...rawdbd.nc``.

    deploymentyaml : str
        YAML text file with deployment information for this glider.

    incremental : bool
        Only add new files....
    """

    def _subset_on_merge(sensor):
        """
        Reduces dataset to timepoints matching variables supplied in datasetyaml
        Parameters
        ----------
        sensor: dataset of data loaded from one or more SeaExplorer pld1 file
        Returns
        -------
        dataset subsampled to only points of interest

        """
        before_subset = len(sensor.time)
        with open(deploymentyaml) as fin:
            deployment = yaml.safe_load(fin)
        ncvar = deployment['netcdf_variables']
        if 'timebase' in ncvar:
            indctd = np.where(~np.isnan(sensor[ncvar['timebase']['source']]))[0]
        elif 'GPCTD_TEMPERATURE' in list(sensor.variables):
            _log.warning('No timebase specified. Using GPCTD_TEMPERATURE as time base')
            indctd = np.where(~np.isnan(sensor.GPCTD_TEMPERATURE))[0]
        elif 'LEGATO_TEMPERATURE' in list(sensor.variables):
            _log.warning('No timebase specified. Using LEGATO_TEMPERATURE as time base')
            indctd = np.where(~np.isnan(sensor.LEGATO_TEMPERATURE))[0]
        else:
            _log.warning('No gpctd or legato data found. Using NAV_DEPTH as time base')
            indctd = np.where(~np.isnan(sensor.NAV_DEPTH))[0]
        sensor = sensor[dict(time=indctd)]
        if 'keep_variables' in ncvar:
            keeps = np.empty(len(sensor.NAV_LONGITUDE))
            keeps[:] = np.nan
            keeper_vars = ncvar['keep_variables']
            for keep_var in keeper_vars:
                keep_source = ncvar[keep_var]['source']
                keeps[~np.isnan(sensor[keep_source].values)] = 1
            sensor = sensor.where(~np.isnan(keeps))
            sensor = sensor.dropna(dim='time', how='all')
        _log.info(f'Input data reduced to {int(100 * len(sensor.time) / before_subset)} %')
        return sensor

    with open(deploymentyaml) as fin:
        deployment = yaml.safe_load(fin)
    metadata = deployment['metadata']
    id = metadata['glider_name']
    outgli = outdir + '/' + id + '-rawgli.nc'
    outpld = outdir + '/' + id + '-' + kind + 'pld.nc'

    _log.info('Opening *.gli.sub.*.nc multi-file dataset from %s', indir)
    files = sorted(glob.glob(indir+'/*.gli.sub.*.nc'))
    if not files:
        _log.warning(f'No *gli*.nc files found in {indir}')
        return False
    gli = xr.open_mfdataset(indir+'/*.gli.sub.*.nc', combine='by_coords', decode_times=False)
    _log.info(f'Writing {outgli}')
    gli.to_netcdf(outgli)
    _log.info(f'Done writing {outgli}')

    _log.info('Opening *.pld.sub.*.nc multi-file dataset')
    files = sorted(glob.glob(indir+'/*.pld1.'+kind+'.*.nc'))
    if not files:
        _log.warning(f'No *{kind}*.nc files found in {indir}')
        return False
    pld = xr.open_mfdataset(indir+'/*.pld1.'+kind+'.*.nc', combine='by_coords', decode_times=False,
                            preprocess=_subset_on_merge)
    _log.info(f'Loading dataset to memory')
    pld.load()
    _log.info(f'Writing {outpld}')
    pld.to_netcdf(outpld)
    _log.info(f'Done writing {outpld}')
    _log.info('Done merge_rawnc')
    return True


def _interp_gli_to_pld(gli, ds, val, indctd):
    gli_ind = ~np.isnan(val)
    valout = np.interp(ds['time'],
                       gli['time'][gli_ind],
                       val[gli_ind])
    return valout


def _interp_pld_to_pld(pld, ds, val, indctd):
    pld_ind = np.where(~np.isnan(val))[0]
    if len(pld_ind) != len(indctd):
        val = np.interp(ds['time'],
                           pld['time'][pld_ind],
                           val[pld_ind])
    else:
        val = val[indctd]
    return val


def raw_to_L0timeseries(indir, outdir, deploymentyaml, kind='raw',
                        profile_filt_time=100, profile_min_time=300):
    """
    A little different than above, for the 4-file version of the data set.
    """

    with open(deploymentyaml) as fin:
        deployment = yaml.safe_load(fin)
    metadata = deployment['metadata']
    ncvar = deployment['netcdf_variables']
    device_data = deployment['glider_devices']
    id = metadata['glider_name']
    _log.info(f'Opening combined nav file {indir}/{id}-rawgli.nc')
    gli = xr.open_dataset(f'{indir}/{id}-rawgli.nc', decode_times=False)
    _log.info(f'Opening combined payload file {indir}/{id}-{kind}pld.nc')
    sensor = xr.open_dataset(f'{indir}/{id}-{kind}pld.nc', decode_times=False)

    # build a new data set based on info in `deploymentyaml.`
    # We will use ctd as the interpolant
    ds = xr.Dataset()
    attr = {}
    name = 'time'
    for atts in ncvar[name].keys():
        if atts != 'coordinates':
            attr[atts] = ncvar[name][atts]

    # If present, use the timebase specified in ncva: timebase in the deployment yaml
    # Otherwise, the ctd will be our timebase.  It oversamples the nav data, but
    # mildly undersamples the optics and oxygen....
    if 'timebase' in ncvar:
        indctd = np.where(~np.isnan(sensor[ncvar['timebase']['source']]))[0]
    elif 'GPCTD_TEMPERATURE' in list(sensor.variables):
        _log.warning('No timebase specified. Using GPCTD_TEMPERATURE as time base')
        indctd = np.where(~np.isnan(sensor.GPCTD_TEMPERATURE))[0]
    elif 'LEGATO_TEMPERATURE' in list(sensor.variables):
        _log.warning('No timebase specified. Using LEGATO_TEMPERATURE as time base')
        indctd = np.where(~np.isnan(sensor.LEGATO_TEMPERATURE))[0]
    else:
        _log.warning('No gpctd or legato data found. Using NAV_DEPTH as time base')
        indctd = np.where(~np.isnan(sensor.NAV_DEPTH))[0]
    ds['time'] = (('time'), sensor['time'].values[indctd], attr)
    thenames = list(ncvar.keys())
    for i in ['time', 'timebase', 'keep_variables']:
        if i in thenames:
            thenames.remove(i)
    for name in thenames:
        _log.info('interpolating ' + name)
        if not('method' in ncvar[name].keys()):
            # variables that are in the data set or can be interpolated from it
            if 'conversion' in ncvar[name].keys():
                convert = getattr(utils, ncvar[name]['conversion'])
            else:
                convert = utils._passthrough
            sensorname = ncvar[name]['source']
            if sensorname in list(sensor.variables):
                _log.debug('sensorname %s', sensorname)
                val = convert(sensor[sensorname])
                if 'coarsen' in ncvar[name]:
                    # smooth oxygen data as originally perscribed
                    coarsen_time = ncvar[name]['coarsen']
                    sensor_sub = sensor.coarsen(time=coarsen_time, boundary='trim').mean()
                    val2 = sensor_sub[sensorname]
                    val = _interp_gli_to_pld(sensor_sub, sensor, val2, indctd)
                val = val[indctd]

                ncvar['method'] = 'linear fill'
            else:
                val = gli[sensorname]
                val = convert(val)
                # Values from the glider netcdf must be interpolated to match the sensor netcdf
                val = _interp_gli_to_pld(gli, ds, val, indctd)

            # make the attributes:
            ncvar[name].pop('coordinates', None)
            attrs = ncvar[name]
            attrs = utils.fill_required_attrs(attrs)
            ds[name] = (('time'), val.data, attrs)

    # fix lon and lat to be linearly interpolated between fixes
    good = np.where(np.abs(np.diff(ds.longitude)) +
                    np.abs(np.diff(ds.latitude)) > 0)[0] + 1
    ds['longitude'].values = np.interp(ds.time,
        ds.time[good], ds.longitude[good])
    ds['latitude'].values = np.interp(ds.time,
        ds.time[good], ds.latitude[good])

    # keep only timestamps with data from one of a set of variables
    if 'keep_variables' in ncvar:
        keeps = np.empty(len(ds.longitude))
        keeps[:] = np.nan
        keeper_vars = ncvar['keep_variables']
        for keep_var in keeper_vars:
            keeps[~np.isnan(ds[keep_var].values)] = 1
        ds = ds.where(~np.isnan(keeps))
        ds = ds.dropna(dim='time', how='all')
    # some derived variables:
    ds = utils.get_glider_depth(ds)
    ds = utils.get_distance_over_ground(ds)
    #    ds = utils.get_profiles(ds)
    ds = utils.get_profiles_new(ds,
            filt_time=profile_filt_time, profile_min_time=profile_min_time)
    ds = utils.get_derived_eos_raw(ds)

    ds = ds.assign_coords(longitude=ds.longitude)
    ds = ds.assign_coords(latitude=ds.latitude)
    ds = ds.assign_coords(depth=ds.depth)
    #ds = ds._get_distance_over_ground(ds)

    ds = utils.fill_metadata(ds, deployment['metadata'], device_data)

    # somehow this comes out unsorted:
    ds = ds.sortby(ds.time)

    start = ((ds['time'].values[0]).astype('timedelta64[s]') +
        np.datetime64('1970-01-01T00:00:00'))
    end = ((ds['time'].values[-1]).astype('timedelta64[s]')  +
        np.datetime64('1970-01-01T00:00:00'))

    ds.attrs['deployment_start'] = str(start)
    ds.attrs['deployment_end'] = str(end)

    try:
        os.mkdir(outdir)
    except:
        pass
    id0 = ds.attrs['deployment_name']
    outname = outdir + id0 + '.nc'
    _log.info('writing %s', outname)
    ds.to_netcdf(outname, 'w')

    return outname

# alias:
raw_to_L1timeseries = raw_to_L0timeseries


def _parse_sensor_config(filename):
    """
    Reads the sensor config file of a SeaExplorer and extracts the active sensors and their calibration data.
    Parameters
    ----------
    filename: path to seapayload.cfg file

    Returns
    -------
    Dictionary of devices and their metadata as dictionaries of key_value pairs
    """
    #filename = "/home/callum/Documents/data-flow/data-in/SEA063_M22/2_PLD/configs/seapayload.cfg"
    file = open(filename, 'r').read().split('\n')
    devices = []
    device_id = 'dummy_value'
    device_dicts = {}
    dict_for_device = {}
    for line in file:
        # Strip trailing whitespace
        line = line.strip(" ")
        # Look for key:value pairs
        if '=' in line and ">" not in line:
            # Split only on first = or AD2CP will break the parser
            key, value = line.split("=", 1)
            # Look for non-empty device declarations
            if key == "device" and value != "":
                devices.append(value)
            else:
                dict_for_device[key] = value
        # Look for pattern [devicename]
        elif line[1:-1] in devices:
            # add previous device to the device dict
            device_dicts[device_id] = dict_for_device
            device_id = line[1:-1]
            dict_for_device = {}
    # Append the final device to the dict
    device_dicts[device_id] = dict_for_device

    active_device_dicts = {k: device_dicts[k] for k in device_dicts.keys() & {*devices}}
    return active_device_dicts
