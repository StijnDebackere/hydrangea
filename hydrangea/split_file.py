"""Provides SplitFile class to read distributed HDF5 data sets."""

import os
import numpy as np
import h5py as h5

from pdb import set_trace
import hydrangea.hdf5 as hd
import hydrangea.tools as ht
from hydrangea.reader_base import ReaderBase
import time


class SplitFile(ReaderBase):
    """Class to read data sets that are split over multiple files.

    Attributes
    ----------
    file_name : str
        Name of one of the files in this split collection.
    group_name : str or None
        Base group to read data from (None if not specified)
    num_elem : int or None
        Number of elements in the selected group category (None if it
        could not be determined or is not applicable).
    num_files : int or None
        Number of files in the collection (None if not determined).
    """

    def __init__(self, file_name, group_name=None, part_type=None,
                 sim_type='Eagle', verbose=1, astro=True, read_range=None,
                 read_index=None):
        """Initialize a file collection to read from.

        Parameters
        ----------
        file_name : str
            Any one of the files in the collection to read.
        group_name : str, optional
            Name of base group to read from. If not provided, it is
            assumed to be 'PartType[x]', where x is the particle type
            (which must then be supplied).
        part_type : int, optional
            The (numerical) particle type; only relevant for sn[a/i]pshots.
            If not provided, it is inferred from group_name, which must
            then be supplied.
        sim_type : str, optional
            Type of simulation to which this data belongs:
            'Eagle' (default) or 'Illustris'.
        verbose : int, optional
            Specify level of log output, from 0 (minimal) to 2 (lots).
            Default: 1.
        astro : bool, optional
            Default conversion behaviour (default: True).
        read_range : (int, int) or None, optional
            Read only elements from the first up to *but excluding* the
            second entry in the tuple. If None (default), load entire
            catalogue. Ignored if read_index is provided.
        read_index : int or np.array(int) or None, optional
            Read only the elements in read_index. If int, a single
            element is read, and the first dimension truncated. If
            an array is provided, the elements between the lowest and
            highest index are read and the output then masked to the
            exact elements. If None (default), everything is read.

        Note
        ----
        For proper functionality, read_range requires that the file
        offsets be determined. If this is not possible, the entire
        data set will be read and then truncated (slower).

        """
        self.verbose = verbose
        self.astro = astro
        self.read_index = read_index
        if read_index is None:
            self.read_range = read_range
        elif isinstance(read_index, int):
            self.read_range = [read_index, read_index+1]
        else:
            self.read_range = [np.min(read_index), np.max(read_index)+1]

        # First: check if the file exists...!
        if not os.path.isfile(file_name):
            print("-----------------------------------------------------")
            print("Error [" + file_name + "]")
            print("This file does not exist. Please supply correct name!")
            print("-----------------------------------------------------")
            return
        self.file_name = file_name
        self.sim_type = sim_type

        # Set up group name, if needed from particle type
        if part_type is not None:
            self._decode_ptype(part_type)
        else:
            self.base_group = group_name

        self._print(1, "Prepared reading from '{:s}'..."
                    .format(self.base_group.upper()))

    def read_data(self, dataset_name, verbose=None, astro=None,
                  return_conv=False, store=False, trial=False):
        """Read a specified data set from the file collection.

        Parameters
        ----------
        dataset_name : str
            The name of the data set to read, including possibly containing
            groups, but *not* the main group specified in the instantiation.
        astro : bool or None, optional
            Attempt conversion to `astronomical' units where necessary
            (pMpc, 10^10 M_sun, km/s). This is ignored for dimensionless
            quantities. Default: True
        verbose : int, optional
            Provide more or less useful messages during reading.
            Default: 1 (minimal)
        store : str or None or False, optional
            Store the retrieved array as an attribute with this name.
            If None, the (full) name of the data set is used, with
            '/' replaced by '__'. Default: False.
        trial : bool, optional
            Attempt to read the data set. If it does not yield the
            expected number of elements for any one file or total
            (or returns any elements in HAC mode), return None.
            If False (default), enter debug mode in this case.

        Returns
        -------
        data : np.array
            Array containing the specified data.
        astro_conv : float or None
            Conversion factor to astronomical units; returned only if
            return_conv is True.

        """
        if astro is None:
            astro = self.astro
        if verbose is None:
            verbose = self.verbose

        # Check that setup has been done properly
        if self.num_files is None:
            print("I don't know how many files to read...")
            set_trace()

        # Read individual files
        need_to_truncate = True   # Check for post-hoc truncation
        if self.num_elem is not None:
            if self.file_offsets is not None:
                data_out = self._read_files_direct(dataset_name,
                                                   verbose, trial)
                need_to_truncate = False  # Done internally here
            else:
                data_out = self._read_files_sequentially(dataset_name,
                                                         verbose, trial)
        else:
            # Use HAC if we don't know how many elements to read in total
            data_out = self._read_files_hac(dataset_name, trial,
                                            verbose=verbose)

        # Apply corrections if needed
        if data_out is not None:
            if need_to_truncate and self.read_range is not None:
                data_out = data_out[self.read_range[0] : self.read_range[1]]
            if isinstance(self.read_index, int):
                if data_out.ndim == 1:
                    data_out = data_out[0]
                else:
                    data_out = data_out[0, ...]
            elif self.read_index is not None:
                ind_sel = self.read_index - self.read_range[0]
                data_out = data_out[ind_sel, ...]
            if astro or return_conv:
                astro_conv = self.get_astro_conv(dataset_name)
                if astro and astro_conv is not None and astro_conv != 1:
                    data_out *= astro_conv

        # Store the array directly in the object, if desired
        if store is not False:
            if store is None:
                store = dataset_name.replace('/', '__')
            setattr(self, store, data_out)

        if return_conv:
            return data_out, astro_conv
        else:
            return data_out

    def _read_files_direct(self, dataset_name, verbose=1, trial=False):
        """Read data set from files using pre-established offset list."""
        data_out = None
        for ifile in range(self.read_start[0], self.read_end[0]+1):

            # Find number of elements to read in this file
            start = 0
            end = self.file_offsets[ifile+1] - self.file_offsets[ifile]
            if self.read_range is not None:
                if ifile == self.read_start[0]:
                    start = self.read_start[1]
                if ifile == self.read_end[0]:
                    end = self.read_end[1]
            num_exp = end - start
            write_offset = (start + self.file_offsets[ifile]
                            - self.read_range[0])

            if num_exp > 0:
                num_read, data_out = self._read_file(
                    ifile, dataset_name, data_out, read_range=[start, end],
                    offset=write_offset, verbose=verbose)
                if num_read != num_exp:
                    if trial:
                        return None
                    print("Read {:d} elements, but expected {:d}!"
                          .format(num_read, num_exp))
                    set_trace()
        self._print((1, verbose), "")  # Ends no-newline sequence
        return data_out

    def _read_files_sequentially(self, dataset_name, verbose=1,
                                 trial=False):
        """Read data from files in sequential order."""
        data_out = None
        offset = 0

        # Read data into data_out, which is cycled through _read_file()
        for ifile in range(self.num_files):
            num_read, data_out = self._read_file(
                ifile, dataset_name, data_out, offset=offset, verbose=verbose)
            offset += num_read

        # Make sure we read right total number
        if offset != self.num_elem:
            if trial:
                return None
            print("Read wrong number of elements for '{:s}'."
                  .format(dataset_name))
            set_trace()

        self._print((1, verbose), "")   # Ends no-newline sequence
        return data_out

    def _read_files_hac(self, dataset_name, trial=False, threshold=int(1e9),
                        verbose=1):
        """Read data set from files into output, using HAC."""
        data_out = None
        data_stack = None
        data_part = None

        for ifile in range(self.num_files):
            num, data_part = self._read_file(ifile, dataset_name, None,
                                             self_only=True, verbose=verbose)
            if num == 0:
                continue

            # Initialize output and stack, or append part to stack
            if data_out is None:
                data_out = np.copy(data_part)
                data_stack = np.copy(data_part)
            else:
                data_stack = np.concatenate((data_stack, data_part))

            # Combine to full list if critical size reached:
            if len(data_stack) > threshold:
                self._print((1, verbose), "Update full list...")
                data_out = np.concatenate((data_out, data_stack))
                empty_shape = list(data_stack.shape)
                empty_shape[0] = 0
                data_stack = np.empty(empty_shape, data_stack.dtype)

        self._print((1, verbose), "")  # Ends no-newline sequence

        # Need to do final concatenation after loop ends
        if data_stack is not None and len(data_stack):
            self._print((1, verbose), "Final concatenation...")
            data_out = np.concatenate((data_out, data_stack))

        if trial and (data_out is None or len(data_out) == 0):
            return None
        return data_out

    def _read_file(self, ifile, dataset_name, data_out, offset=0,
                   verbose=1, read_range=None, num_out=None,
                   self_only=False):
        """Read specified data set from one file into output.

        This is the low-level reading routine called by the three
        'driver' functions _read_files_... above.

        Parameters
        ----------
        ifile : int
            The index of the file to read.
        dataset_name : str
            The name of the data set to read, including possibly containing
            groups but *not* the main base group (e.g. 'PartType0').
        data_out : np.array or None
            The output array. If None, it is initialized internally.
        offset : int, optional
            The offset into the output array to write the data to.
            Default is 0, i.e. fill output array from its beginning.
        verbose : int, optional
            Print more or fewer progress messages.
        read_range : int [start, end] or None, optional
            The range of the data to read. If None (default), read all.
        num_out : int or None, optional
            The number of elements to allocate in the output array, if
            it is initialized internally. If None (default), use self.numElem.
        self_only : bool, optional
            If output is internally initialized, only allocate enough
            elements for data from this file (default: False).

        Returns
        -------
        length : int
            The number of elements that were read.
        data_out : np.array or None
            The updated (or newly initialized) output array.
        """
        self._print((1, verbose), str(ifile) + " ", end="", flush=True)

        # Form current file name and check it exists
        file_name = self._swap_file_name(self.file_name, ifile)
        if not os.path.isfile(file_name):
            print("\nError: did not find expected file {:d}."
                  .format(ifile), flush=True)
            set_trace()

        f = h5.File(file_name, 'r')
        full_dataset_name = self.base_group + '/' + dataset_name

        # Not all files contain all groups, so need to check explicitly
        try:
            dataSet = f[full_dataset_name]
        except KeyError:
            self._print((1, verbose), "No data found on file {:d}!"
                        .format(ifile))
            return 0, data_out

        # With read range, length is pre-determined
        if read_range is not None:
            length = read_range[1] - read_range[0]
            source_sel = np.s_[read_range[0]:read_range[1]]
        else:
            source_sel = None

        if data_out is None:
            if offset > 0:
                self._print(
                    (1, verbose), "Warning: initializing output although "
                    "offset is {:d}." .format(offset))
            shape = list(dataSet.shape)   # Need to modify, so must be list

            if read_range is None:
                length = shape[0]

            if not self_only:
                if num_out is None:
                    shape[0] = self.num_elem
                else:
                    shape[0] = num_out
            data_out = np.empty(shape, dataSet.dtype)
        else:
            if read_range is None:
                length = dataSet.len()
        dataSet.read_direct(data_out, source_sel,
                            dest_sel=np.s_[offset:offset+length, ...])

        return length, data_out

    def _decode_ptype(self, ptype):
        """Identify supplied particle type."""
        if isinstance(ptype, int):
            self.base_group = 'PartType{:d}' .format(ptype)
        elif isinstance(ptype, str):
            if ptype.upper() == 'GAS':
                self.base_group = 'PartType0'
            elif ptype.upper() == 'DM':
                self.base_group = 'PartType1'
            elif ptype.upper() in ['STARS', 'STAR']:
                self.base_group = 'PartType4'
            elif ptype.upper() in ['BH', 'BHS', 'BLACKHOLE', 'BLACKHOLES',
                                   'BLACK_HOLES', 'BLACK_HOLE']:
                self.base_group = 'PartType5'
            else:
                print("Unrecognized particle type name '{:s}'"
                      .format(ptype))
                set_trace()
        else:
            print("Unrecognized particle type format")
            set_trace()

    @property
    def num_elem(self):
        """Find out how many output elements there are in total.

        Note that, with read_range set up, this is the total number
        of elements in this range, not in the total catalogue.
        """
        if '_num_elem' not in dir(self):
            self._print(2, "Load total number of elements...")
            if self.read_range is None:
                self._num_elem = self._count_elements()
            else:
                self._num_elem = self.read_range[1]-self.read_range[0]
        return self._num_elem

    def _count_elements(self, file=None):
        """Count number of elements to read."""
        num_elem = None   # Placeholder for "not known"
        if self.base_group is None:
            return

        # Break file name into base and running sequence number
        real_file_name = os.path.split(self.file_name)[1]
        file_name_parts = real_file_name.split('_')

        if self.base_group.startswith('PartType'):
            pt_index = int(self.base_group[8])
        else:
            pt_index = None   # For checking

        # Deal with particle catalogue files
        if (file_name_parts[0] in ['snap', 'snip', 'partMags',
                                   'eagle_subfind_particles']):
            if file is None:
                self._print(2, "   Particle catalogue detected... ", end="")
            # Need to extract particle index to count (mag --> stars!)
            if self.base_group.startswith('PartType'):
                if file is None:
                    num_elem = self._count_elements_snap(pt_index)
                else:
                    num_elem = self._count_file_elements_snap(file, pt_index)
            elif file_name_parts[0] == 'partMags':
                if file is None:
                    num_elem = self._count_elements_snap(4)
                else:
                    num_elem = self._count_file_elements_snap(file, 4)
            else:
                return    # Can't determine element numbers then

        # Deal with subfind catalogue files
        elif len(file_name_parts) >= 2:
            if "_".join(file_name_parts[:3]) == 'eagle_subfind_tab':
                if file is None:
                    self._print(2, "   Subfind catalogue detected... ", end="")
                if self.base_group == 'FOF':
                    if file is None:
                        num_elem = self._count_elements_sf_fof()
                    else:
                        num_elem = self._count_file_elements_sf_fof(file)
                elif self.base_group == 'Subhalo':
                    if file is None:
                        num_elem = self._count_elements_sf_subhalo()
                    else:
                        num_elem = self._count_file_elements_sf_subhalo(file)
                elif self.base_group == 'IDs':
                    if file is None:
                        num_elem = self._count_elements_sf_ids()
                    else:
                        num_elem = self._count_file_elements_sf_ids(file)
                else:
                    return

        # In all other cases, don't know how to pre-determine
        # element count, so need to find out from individual files (later).
        else:
            return

        # Final piece: deal with possibility of int32 overflow in numbers
        # (it does happen somewhere...)
        if num_elem < 0:
            num_elem += 4294967296
        if file is None:
            self._print(2, "{:d} elements." .format(num_elem), flush=True)
        return num_elem

    def _count_elements_snap(self, pt_index):
        """Count number of particles of specified (int) type."""
        return hd.read_attribute(
            self.file_name, 'Header', 'NumPart_Total', require=True)[pt_index]

    def _count_elements_sf_fof(self):
        """Count number of FOF groups in Subfind catalogue."""
        return hd.read_attribute(
            self.file_name, 'Header', 'TotNgroups', require=True)

    def _count_elements_sf_subhalo(self):
        """Count number of subhaloes in Subfind catalogue."""
        return hd.read_attribute(
            self.file_name, 'Header', 'TotNsubgroups', require=True)

    def _count_elements_sf_ids(self):
        """Count number of particle IDs in Subfind catalogue."""
        return hd.read_attribute(
            self.file_name, 'Header', 'TotNids', require=True)

    def _count_file_elements_snap(self, file, pt_index):
        """Count number of particles of specified (int) type."""
        return hd.read_attribute(
            self._swap_file_name(self.file_name, file),
            'Header', 'NumPart_ThisFile', require=True)[pt_index]

    def _count_file_elements_sf_fof(self, file):
        """Count number of FOF groups in Subfind catalogue."""
        return hd.read_attribute(
            self._swap_file_name(self.file_name, file),
            'Header', 'Ngroups', require=True)

    def _count_file_elements_sf_subhalo(self, file):
        """Count number of subhaloes in Subfind catalogue."""
        return hd.read_attribute(
            self._swap_file_name(self.file_name, file),
            'Header', 'Nsubgroups', require=True)

    def _count_file_elements_sf_ids(self, file):
        """Count number of particle IDs in Subfind catalogue."""
        return hd.read_attribute(
            self._swap_file_name(self.file_name, file),
            'Header', 'Nids', require=True)

    @property
    def num_files(self):
        """Count number of files in the data set."""
        if '_num_files' not in dir(self):
            self._print(2, "Loading file offsets...")
            if self.sim_type == 'Eagle':
                self._num_files = hd.read_attribute(
                    self.file_name, 'Header', 'NumFilesPerSnapshot',
                    require=True)
            elif self.sim_type == 'Illustris':
                self._num_files = hd.read_attribute(
                    self.file_name, 'Header', 'NumFiles', require=True)
            else:
                print("Unknown simulation type '{:s}'." .format(self.sim_type))
                set_trace()
        return self._num_files

    @property
    def file_offsets(self):
        """List offset of each file in total data set."""
        if '_file_offsets' not in dir(self):
            start_time = time.time()
            pm_file_name = (os.path.dirname(self.file_name)
                            + '/ParticleMap.hdf5')
            if os.path.exists(pm_file_name):
                self._print(2, "Loading file offsets from map...")
                self._find_file_offsets_from_map(pm_file_name)
            else:
                self._print(2, "Finding file offsets one-by-one.")
                self._find_file_offsets()
            self._print(1, "Loaded file offsets in {:.3f} sec."
                        .format(time.time() - start_time))
        return self._file_offsets

    def _find_file_offsets_from_map(self, map_file_name):
        """Extract offsets of each file in total data set from map."""
        self._file_offsets = hd.read_data(
            map_file_name, self.base_group + '/FileOffset')

    def _find_file_offsets(self):
        """Find file offsets sequentially from individual files."""
        self._file_offsets = np.zeros(self.num_files + 1, dtype=int)
        for ifile in range(self.num_files):
            length = self._count_elements(ifile)

            # Abandon ship if even one file cannot be measured
            if length is None:
                self._file_offsets = None
                return

            self._file_offsets[ifile+1] = self._file_offsets[ifile] + length

    @property
    def read_start(self):
        """File and offset of first element to read."""
        if '_read_start' not in dir(self):
            if self.read_range is None:
                self._read_start = (0, 0)
            else:
                self._read_start = self._locate_index(self.read_range[0])
                self._print(2, "Read start: (file={:d}, offset={:d})"
                            .format(*self._read_start))
        return self._read_start

    @property
    def read_end(self):
        """File and offset of last element to read."""
        if '_read_end' not in dir(self):
            if self.read_range is None:
                self._read_end = (self.num_files-1, None)
            else:
                self._read_end = self._locate_index(self.read_range[1])
                self._print(2, "Read end: (file={:d}, offset={:d})"
                            .format(*self._read_end))
        return self._read_end

    def _locate_index(self, index):
        """Locate the file and offset of a specified element."""
        file = np.searchsorted(self.file_offsets, index, side='right') - 1
        offset = index - self.file_offsets[file]
        if file < 0:
            self._print(1, "Truncating lower read end to 0 (was {:d})."
                        .format(index))
            file = offset = 0
        if file >= self.num_files:
            self._print(1, "Truncating upper read end to {:d} (was {:d})"
                        .format(self.file_offsets[-1]))
        return (file, offset)
