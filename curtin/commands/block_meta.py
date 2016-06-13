#   Copyright (C) 2013 Canonical Ltd.
#
#   Author: Scott Moser <scott.moser@canonical.com>
#
#   Curtin is free software: you can redistribute it and/or modify it under
#   the terms of the GNU Affero General Public License as published by the
#   Free Software Foundation, either version 3 of the License, or (at your
#   option) any later version.
#
#   Curtin is distributed in the hope that it will be useful, but WITHOUT ANY
#   WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
#   FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for
#   more details.
#
#   You should have received a copy of the GNU Affero General Public License
#   along with Curtin.  If not, see <http://www.gnu.org/licenses/>.

from collections import OrderedDict
from curtin import (block, config, util)
from curtin.block import (mdadm, mkfs, clear_holders)
from curtin.log import LOG
from curtin.reporter import events

from . import populate_one_subcmd
from curtin.udev import compose_udev_equality, udevadm_settle

import glob
import os
import platform
import sys
import tempfile
import time

SIMPLE = 'simple'
SIMPLE_BOOT = 'simple-boot'
CUSTOM = 'custom'

CMD_ARGUMENTS = (
    ((('-D', '--devices'),
      {'help': 'which devices to operate on', 'action': 'append',
       'metavar': 'DEVICE', 'default': None, }),
     ('--fstype', {'help': 'root partition filesystem type',
                   'choices': ['ext4', 'ext3'], 'default': 'ext4'}),
     (('-t', '--target'),
      {'help': 'chroot to target. default is env[TARGET_MOUNT_POINT]',
       'action': 'store', 'metavar': 'TARGET',
       'default': os.environ.get('TARGET_MOUNT_POINT')}),
     ('--boot-fstype', {'help': 'boot partition filesystem type',
                        'choices': ['ext4', 'ext3'], 'default': None}),
     ('mode', {'help': 'meta-mode to use',
               'choices': [CUSTOM, SIMPLE, SIMPLE_BOOT]}),
     )
)


def block_meta(args):
    # main entry point for the block-meta command.
    state = util.load_command_environment()
    cfg = config.load_command_config(args, state)
    if args.mode == CUSTOM or cfg.get("storage") is not None:
        meta_custom(args)
    elif args.mode in (SIMPLE, SIMPLE_BOOT):
        meta_simple(args)
    else:
        raise NotImplementedError("mode=%s is not implemented" % args.mode)


def logtime(msg, func, *args, **kwargs):
    with util.LogTimer(LOG.debug, msg):
        return func(*args, **kwargs)


def write_image_to_disk(source, dev):
    """
    Write disk image to block device
    """
    (devname, devnode) = block.get_dev_name_entry(dev)
    util.subp(args=['sh', '-c',
                    ('wget "$1" --progress=dot:mega -O - |'
                     'tar -SxOzf - | dd of="$2"'),
                    '--', source, devnode])
    util.subp(['partprobe', devnode])
    udevadm_settle()
    return block.get_root_device([devname, ])


def get_bootpt_cfg(cfg, enabled=False, fstype=None, root_fstype=None):
    # 'cfg' looks like:
    #   enabled: boolean
    #   fstype: filesystem type (default to 'fstype')
    #   label:  filesystem label (default to 'boot')
    # parm enable can enable, but not disable
    # parm fstype overrides cfg['fstype']
    def_boot = (platform.machine() in ('aarch64') and
                not util.is_uefi_bootable())
    ret = {'enabled': def_boot, 'fstype': None, 'label': 'boot'}
    ret.update(cfg)
    if enabled:
        ret['enabled'] = True

    if ret['enabled'] and not ret['fstype']:
        if root_fstype:
            ret['fstype'] = root_fstype
        if fstype:
            ret['fstype'] = fstype
    return ret


def get_partition_format_type(cfg, machine=None, uefi_bootable=None):
    if machine is None:
        machine = platform.machine()
    if uefi_bootable is None:
        uefi_bootable = util.is_uefi_bootable()

    cfgval = cfg.get('format', None)
    if cfgval:
        return cfgval

    if uefi_bootable:
        return 'uefi'

    if machine in ['aarch64']:
        return 'gpt'
    elif machine.startswith('ppc64'):
        return 'prep'

    return "mbr"


def old_clear_holders(sys_block_path):
    """
    Shutdown all storage layers holding device in /sys/block path.
    Ignore IO errors encountered, as installation should be able to continue in
    many cases when not everything was able to be shut down.
    """
    holders = clear_holders.get_holders(sysfs_path=sys_block_path)
    LOG.info("clear_holders running on '%s', with holders '%s'" %
             (sys_block_path, holders))
    for holder in holders:
        # get path to holder in /sys/block, then clear it
        # ignore io errors as something may have already caused the holder
        # device to go away
        with util.forgive_io_err("could not find {}".format(holder)):
            holder_realpath = os.path.realpath(
                os.path.join(sys_block_path, "holders", holder))
            clear_holders(holder_realpath)

    # detect what type of holder is using this volume and shut it down, need to
    # find more robust name of doing detection
    if "bcache" in sys_block_path:
        # bcache device
        part_devs = []
        for part_dev in glob.glob(os.path.join(sys_block_path,
                                               "slaves", "*", "dev")):
            with open(part_dev, "r") as fp:
                part_dev_id = fp.read().rstrip()
                part_devs.append(
                    os.path.split(os.path.realpath(os.path.join("/dev/block",
                                  part_dev_id)))[-1])
        for cache_dev in glob.glob("/sys/fs/bcache/*/bdev*"):
            for part_dev in part_devs:
                if part_dev in os.path.realpath(cache_dev):
                    # This is our bcache device, stop it, wait for udev to
                    # settle
                    with open(os.path.join(os.path.split(cache_dev)[0],
                              "stop"), "w") as fp:
                        LOG.info("stopping: %s" % fp)
                        fp.write("1")
                        udevadm_settle()
                    break
        for part_dev in part_devs:
            block.wipe_volume(os.path.join("/dev", part_dev),
                              mode="superblock")

    if os.path.exists(os.path.join(sys_block_path, "bcache")):
        # bcache device that isn't running, if it were, we would have found it
        # when we looked for holders
        try:
            with open(os.path.join(sys_block_path, "bcache", "set", "stop"),
                      "w") as fp:
                LOG.info("stopping: %s" % fp)
                fp.write("1")
        except IOError as e:
            if not util.is_file_not_found_exc(e):
                raise e
            with open(os.path.join(sys_block_path, "bcache", "stop"),
                      "w") as fp:
                LOG.info("stopping: %s" % fp)
                fp.write("1")
        udevadm_settle()


def devsync(devpath):
    LOG.debug('devsync for %s', devpath)
    util.subp(['partprobe', devpath], rcs=[0, 1])
    udevadm_settle()
    for x in range(0, 10):
        if os.path.exists(devpath):
            LOG.debug('devsync happy - path %s now exists', devpath)
            return
        else:
            LOG.debug('Waiting on device path: %s', devpath)
            time.sleep(1)
    raise OSError('Failed to find device at path: %s', devpath)


def determine_partition_kname(disk_kname, partition_number):
    for dev_type in ["nvme", "mmcblk"]:
        if disk_kname.startswith(dev_type):
            partition_number = "p%s" % partition_number
            break
    return "%s%s" % (disk_kname, partition_number)


def determine_partition_number(partition_id, storage_config):
    vol = storage_config.get(partition_id)
    partnumber = vol.get('number')
    if vol.get('flag') == "logical":
        if not partnumber:
            LOG.warn('partition \'number\' key not set in config:\n%s',
                     util.json_dumps(vol))
            partnumber = 5
            for key, item in storage_config.items():
                if item.get('type') == "partition" and \
                        item.get('device') == vol.get('device') and\
                        item.get('flag') == "logical":
                    if item.get('id') == vol.get('id'):
                        break
                    else:
                        partnumber += 1
    else:
        if not partnumber:
            LOG.warn('partition \'number\' key not set in config:\n%s',
                     util.json_dumps(vol))
            partnumber = 1
            for key, item in storage_config.items():
                if item.get('type') == "partition" and \
                        item.get('device') == vol.get('device'):
                    if item.get('id') == vol.get('id'):
                        break
                    else:
                        partnumber += 1
    return partnumber


def make_dname(volume, storage_config):
    state = util.load_command_environment()
    rules_dir = os.path.join(state['scratch'], "rules.d")
    vol = storage_config.get(volume)
    path = get_path_to_storage_volume(volume, storage_config)
    ptuuid = None
    dname = vol.get('name')
    if vol.get('type') in ["partition", "disk"]:
        (out, _err) = util.subp(["blkid", "-o", "export", path], capture=True,
                                rcs=[0, 2], retries=[1, 1, 1])
        for line in out.splitlines():
            if "PTUUID" in line or "PARTUUID" in line:
                ptuuid = line.split('=')[-1]
                break
    # we may not always be able to find a uniq identifier on devices with names
    if not ptuuid and vol.get('type') in ["disk", "partition"]:
        LOG.warning("Can't find a uuid for volume: {}. Skipping dname.".format(
            dname))
        return

    rule = [
        compose_udev_equality("SUBSYSTEM", "block"),
        compose_udev_equality("ACTION", "add|change"),
        ]
    if vol.get('type') == "disk":
        rule.append(compose_udev_equality('ENV{DEVTYPE}', "disk"))
        rule.append(compose_udev_equality('ENV{ID_PART_TABLE_UUID}', ptuuid))
    elif vol.get('type') == "partition":
        rule.append(compose_udev_equality('ENV{DEVTYPE}', "partition"))
        dname = storage_config.get(vol.get('device')).get('name') + \
            "-part%s" % determine_partition_number(volume, storage_config)
        rule.append(compose_udev_equality('ENV{ID_PART_ENTRY_UUID}', ptuuid))
    elif vol.get('type') == "raid":
        md_data = mdadm.mdadm_query_detail(path)
        md_uuid = md_data.get('MD_UUID')
        rule.append(compose_udev_equality("ENV{MD_UUID}", md_uuid))
    elif vol.get('type') == "bcache":
        rule.append(compose_udev_equality("ENV{DEVNAME}", path))
    elif vol.get('type') == "lvm_partition":
        volgroup_name = storage_config.get(vol.get('volgroup')).get('name')
        dname = "%s-%s" % (volgroup_name, dname)
        rule.append(compose_udev_equality("ENV{DM_NAME}", dname))
    rule.append("SYMLINK+=\"disk/by-dname/%s\"" % dname)
    LOG.debug("Writing dname udev rule '{}'".format(str(rule)))
    util.ensure_dir(rules_dir)
    with open(os.path.join(rules_dir, volume), "w") as fp:
        fp.write(', '.join(rule))


def get_path_to_storage_volume(volume, storage_config):
    # Get path to block device for volume. Volume param should refer to id of
    # volume in storage config

    LOG.debug('get_path_to_storage_volume for volume {}'.format(volume))
    devsync_vol = None
    vol = storage_config.get(volume)
    if not vol:
        raise ValueError("volume with id '%s' not found" % volume)

    # Find path to block device
    if vol.get('type') == "partition":
        partnumber = determine_partition_number(vol.get('id'), storage_config)
        disk_block_path = get_path_to_storage_volume(vol.get('device'),
                                                     storage_config)
        (base_path, disk_kname) = os.path.split(disk_block_path)
        partition_kname = determine_partition_kname(disk_kname, partnumber)
        volume_path = os.path.join(base_path, partition_kname)
        devsync_vol = os.path.join(disk_block_path)

    elif vol.get('type') == "disk":
        # Get path to block device for disk. Device_id param should refer
        # to id of device in storage config
        if vol.get('serial'):
            volume_path = block.lookup_disk(vol.get('serial'))
        elif vol.get('path'):
            volume_path = vol.get('path')
        else:
            raise ValueError("serial number or path to block dev must be \
                specified to identify disk")

    elif vol.get('type') == "lvm_partition":
        # For lvm partitions, a directory in /dev/ should be present with the
        # name of the volgroup the partition belongs to. We can simply append
        # the id of the lvm partition to the path of that directory
        volgroup = storage_config.get(vol.get('volgroup'))
        if not volgroup:
            raise ValueError("lvm volume group '%s' could not be found"
                             % vol.get('volgroup'))
        volume_path = os.path.join("/dev/", volgroup.get('name'),
                                   vol.get('name'))

    elif vol.get('type') == "dm_crypt":
        # For dm_crypted partitions, unencrypted block device is at
        # /dev/mapper/<dm_name>
        dm_name = vol.get('dm_name')
        if not dm_name:
            dm_name = vol.get('id')
        volume_path = os.path.join("/dev", "mapper", dm_name)

    elif vol.get('type') == "raid":
        # For raid partitions, block device is at /dev/mdX
        name = vol.get('name')
        volume_path = os.path.join("/dev", name)

    elif vol.get('type') == "bcache":
        # For bcache setups, the only reliable way to determine the name of the
        # block device is to look in all /sys/block/bcacheX/ dirs and see what
        # block devs are in the slaves dir there. Then, those blockdevs can be
        # checked against the kname of the devs in the config for the desired
        # bcache device. This is not very elegant though
        backing_device_kname = os.path.split(get_path_to_storage_volume(
            vol.get('backing_device'), storage_config))[-1]
        sys_path = list(filter(lambda x: backing_device_kname in x,
                               glob.glob("/sys/block/bcache*/slaves/*")))[0]
        while "bcache" not in os.path.split(sys_path)[-1]:
            sys_path = os.path.split(sys_path)[0]
        volume_path = os.path.join("/dev", os.path.split(sys_path)[-1])
        LOG.debug('got bcache volume path {}'.format(volume_path))

    else:
        raise NotImplementedError("cannot determine the path to storage \
            volume '%s' with type '%s'" % (volume, vol.get('type')))

    # sync devices
    if not devsync_vol:
        devsync_vol = volume_path
    devsync(devsync_vol)

    LOG.debug('return volume path {}'.format(volume_path))
    return volume_path


def disk_handler(info, storage_config):
    ptable = info.get('ptable')

    disk = get_path_to_storage_volume(info.get('id'), storage_config)

    # Handle preserve flag
    if info.get('preserve'):
        if not ptable:
            # Don't need to check state, return
            return

        # Check state of current ptable
        try:
            (out, _err) = util.subp(["blkid", "-o", "export", disk],
                                    capture=True)
        except util.ProcessExecutionError:
            raise ValueError("disk '%s' has no readable partition table or \
                cannot be accessed, but preserve is set to true, so cannot \
                continue")
        current_ptable = list(filter(lambda x: "PTTYPE" in x,
                                     out.splitlines()))[0].split("=")[-1]
        if current_ptable == "dos" and ptable != "msdos" or \
                current_ptable == "gpt" and ptable != "gpt":
            raise ValueError("disk '%s' does not have correct \
                partition table, but preserve is set to true, so not \
                creating table, so not creating table." % info.get('id'))
        LOG.info("disk '%s' marked to be preserved, so keeping partition \
                 table")
        return

    # Wipe the disk
    if info.get('wipe') and info.get('wipe') != "none":
        # The disk has a lable, clear all partitions
        mdadm.mdadm_assemble(scan=True)
        disk_kname = os.path.split(disk)[-1]
        syspath_partitions = list(
            os.path.split(prt)[0] for prt in
            glob.glob("/sys/block/%s/*/partition" % disk_kname))
        for partition in syspath_partitions:
            clear_holders.check_clear(partition)
            # in some cases the block dev may not be available when it is
            # to be erased. since it is possible that installation can
            # still continue without having wiped the partition, don't
            # crash here (LP:1579572)
            catcher = util.ForgiveIoError()
            with catcher:
                with open(os.path.join(partition, "dev"), "r") as fp:
                    block_no = fp.read().rstrip()
                partition_path = os.path.realpath(
                    os.path.join("/dev/block", block_no))
                block.wipe_volume(partition_path, mode=info.get('wipe'))
            for e in catcher.caught:
                LOG.warn('while running wipe_volume, caught: {}'.format(e))

        clear_holders.check_clear(disk)
        block.wipe_volume(disk, mode=info.get('wipe'))

    # Create partition table on disk
    if info.get('ptable'):
        LOG.info("labeling device: '%s' with '%s' partition table", disk,
                 ptable)
        if ptable == "gpt":
            util.subp(["sgdisk", "--clear", disk])
        elif ptable == "msdos":
            util.subp(["parted", disk, "--script", "mklabel", "msdos"])

    # Make the name if needed
    if info.get('name'):
        make_dname(info.get('id'), storage_config)


def getnumberoflogicaldisks(device, storage_config):
    logicaldisks = 0
    for key, item in storage_config.items():
        if item.get('device') == device and item.get('flag') == "logical":
            logicaldisks = logicaldisks + 1
    return logicaldisks


def find_previous_partition(disk_id, part_id, storage_config):
    last_partnum = None
    for item_id, command in storage_config.items():
        if item_id == part_id:
            break

        # skip anything not on this disk, not a 'partition' or 'extended'
        if command['type'] != 'partition' or command['device'] != disk_id:
            continue
        if command.get('flag') == "extended":
            continue

        last_partnum = determine_partition_number(item_id, storage_config)

    return last_partnum


def partition_handler(info, storage_config):
    device = info.get('device')
    size = info.get('size')
    flag = info.get('flag')
    disk_ptable = storage_config.get(device).get('ptable')
    partition_type = None
    if not device:
        raise ValueError("device must be set for partition to be created")
    if not size:
        raise ValueError("size must be specified for partition to be created")

    disk = get_path_to_storage_volume(device, storage_config)
    partnumber = determine_partition_number(info.get('id'), storage_config)

    disk_kname = os.path.split(
        get_path_to_storage_volume(device, storage_config))[-1]
    # consider the disks logical sector size when calculating sectors
    try:
        prefix = "/sys/block/%s/queue/" % disk_kname
        with open(prefix + "logical_block_size", "r") as f:
            l = f.readline()
            logical_block_size_bytes = int(l)
    except:
        logical_block_size_bytes = 512
    LOG.debug(
        "{} logical_block_size_bytes: {}".format(disk_kname,
                                                 logical_block_size_bytes))

    if partnumber > 1:
        if partnumber == 5 and disk_ptable == "msdos":
            for key, item in storage_config.items():
                if item.get('type') == "partition" and \
                        item.get('device') == device and \
                        item.get('flag') == "extended":
                    extended_part_no = determine_partition_number(
                        key, storage_config)
                    break
            partition_kname = determine_partition_kname(
                disk_kname, extended_part_no)
            previous_partition = "/sys/block/%s/%s/" % \
                (disk_kname, partition_kname)
        else:
            pnum = find_previous_partition(device, info['id'], storage_config)
            LOG.debug("previous partition number for '%s' found to be '%s'",
                      info.get('id'), pnum)
            partition_kname = determine_partition_kname(disk_kname, pnum)
            previous_partition = "/sys/block/%s/%s/" % \
                (disk_kname, partition_kname)
        LOG.debug("previous partition: {}".format(previous_partition))
        # XXX: sys/block/X/{size,start} is *ALWAYS* in 512b value
        previous_size = util.load_file(os.path.join(previous_partition,
                                                    "size"))
        previous_size_sectors = (int(previous_size) * 512 /
                                 logical_block_size_bytes)
        previous_start = util.load_file(os.path.join(previous_partition,
                                                     "start"))
        previous_start_sectors = (int(previous_start) * 512 /
                                  logical_block_size_bytes)
        LOG.debug("previous partition.size_sectors: {}".format(
                  previous_size_sectors))
        LOG.debug("previous partition.start_sectors: {}".format(
                  previous_start_sectors))

    # Align to 1M at the beginning of the disk and at logical partitions
    alignment_offset = int((1 << 20) / logical_block_size_bytes)
    if partnumber == 1:
        # start of disk
        offset_sectors = alignment_offset
    else:
        # further partitions
        if disk_ptable == "gpt" or flag != "logical":
            # msdos primary and any gpt part start after former partition end
            offset_sectors = previous_start_sectors + previous_size_sectors
        else:
            # msdos extended/logical partitions
            if flag == "logical":
                if partnumber == 5:
                    # First logical partition
                    # start at extended partition start + alignment_offset
                    offset_sectors = (previous_start_sectors +
                                      alignment_offset)
                else:
                    # Further logical partitions
                    # start at former logical partition end + alignment_offset
                    offset_sectors = (previous_start_sectors +
                                      previous_size_sectors +
                                      alignment_offset)

    length_bytes = util.human2bytes(size)
    # start sector is part of the sectors that define the partitions size
    # so length has to be "size in sectors - 1"
    length_sectors = int(length_bytes / logical_block_size_bytes) - 1
    # logical partitions can't share their start sector with the extended
    # partition and logical partitions can't go head-to-head, so we have to
    # realign and for that increase size as required
    if info.get('flag') == "extended":
        logdisks = getnumberoflogicaldisks(device, storage_config)
        length_sectors = length_sectors + (logdisks * alignment_offset)

    # Handle preserve flag
    if info.get('preserve'):
        return
    elif storage_config.get(device).get('preserve'):
        raise NotImplementedError("Partition '%s' is not marked to be \
            preserved, but device '%s' is. At this time, preserving devices \
            but not also the partitions on the devices is not supported, \
            because of the possibility of damaging partitions intended to be \
            preserved." % (info.get('id'), device))

    # Set flag
    # 'sgdisk --list-types'
    sgdisk_flags = {"boot": 'ef00',
                    "lvm": '8e00',
                    "raid": 'fd00',
                    "bios_grub": 'ef02',
                    "prep": '4100',
                    "swap": '8200',
                    "home": '8302',
                    "linux": '8300'}

    LOG.info("adding partition '%s' to disk '%s' (ptable: '%s')",
             info.get('id'), device, disk_ptable)
    LOG.debug("partnum: %s offset_sectors: %s length_sectors: %s",
              partnumber, offset_sectors, length_sectors)
    if disk_ptable == "msdos":
        if flag in ["extended", "logical", "primary"]:
            partition_type = flag
        else:
            partition_type = "primary"
        cmd = ["parted", disk, "--script", "mkpart", partition_type,
               "%ss" % offset_sectors, "%ss" % str(offset_sectors +
                                                   length_sectors)]
        util.subp(cmd, capture=True)
    elif disk_ptable == "gpt":
        if flag and flag in sgdisk_flags:
            typecode = sgdisk_flags[flag]
        else:
            typecode = sgdisk_flags['linux']
        cmd = ["sgdisk", "--new", "%s:%s:%s" % (partnumber, offset_sectors,
               length_sectors + offset_sectors),
               "--typecode=%s:%s" % (partnumber, typecode), disk]
        util.subp(cmd, capture=True)
    else:
        raise ValueError("parent partition has invalid partition table")

    # Wipe the partition if told to do so, do not wipe dos extended partitions
    # as this may damage the disk partition table
    if info.get('wipe') and info.get('wipe') != "none":
        if info.get('flag') == "extended":
            LOG.warn("not wiping extended partition %s" % info.get('id'))
        else:
            block.wipe_volume(
                get_path_to_storage_volume(info.get('id'), storage_config),
                mode=info.get('wipe'))
    # Make the name if needed
    if storage_config.get(device).get('name') and partition_type != 'extended':
        make_dname(info.get('id'), storage_config)


def format_handler(info, storage_config):
    volume = info.get('volume')
    if not volume:
        raise ValueError("volume must be specified for partition '%s'" %
                         info.get('id'))

    # Get path to volume
    volume_path = get_path_to_storage_volume(volume, storage_config)

    # Handle preserve flag
    if info.get('preserve'):
        # Volume marked to be preserved, not formatting
        return

    # Make filesystem using block library
    LOG.debug("mkfs {} info: {}".format(volume_path, info))
    mkfs.mkfs_from_config(volume_path, info)


def mount_handler(info, storage_config):
    state = util.load_command_environment()
    path = info.get('path')
    filesystem = storage_config.get(info.get('device'))
    if not path and filesystem.get('fstype') != "swap":
        raise ValueError("path to mountpoint must be specified")
    volume = storage_config.get(filesystem.get('volume'))

    # Get path to volume
    volume_path = get_path_to_storage_volume(filesystem.get('volume'),
                                             storage_config)

    if filesystem.get('fstype') != "swap":
        # Figure out what point should be
        while len(path) > 0 and path[0] == "/":
            path = path[1:]
        mount_point = os.path.join(state['target'], path)

        # Create mount point if does not exist
        util.ensure_dir(mount_point)

        # Mount volume
        util.subp(['mount', volume_path, mount_point])

    # Add volume to fstab
    if state['fstab']:
        with open(state['fstab'], "a") as fp:
            if volume.get('type') in ["raid", "bcache",
                                      "disk", "lvm_partition"]:
                location = get_path_to_storage_volume(volume.get('id'),
                                                      storage_config)
            elif volume.get('type') in ["partition", "dm_crypt"]:
                location = "UUID=%s" % block.get_volume_uuid(volume_path)
            else:
                raise ValueError("cannot write fstab for volume type '%s'" %
                                 volume.get("type"))

            if filesystem.get('fstype') == "swap":
                path = "none"
                options = "sw"
            else:
                path = "/%s" % path
                options = "defaults"

            if filesystem.get('fstype') in ["fat", "fat12", "fat16", "fat32",
                                            "fat64"]:
                fstype = "vfat"
            else:
                fstype = filesystem.get('fstype')
            fp.write("%s %s %s %s 0 0\n" % (location, path, fstype, options))
    else:
        LOG.info("fstab not in environment, so not writing")


def lvm_volgroup_handler(info, storage_config):
    devices = info.get('devices')
    device_paths = []
    name = info.get('name')
    if not devices:
        raise ValueError("devices for volgroup '%s' must be specified" %
                         info.get('id'))
    if not name:
        raise ValueError("name for volgroups needs to be specified")

    for device_id in devices:
        device = storage_config.get(device_id)
        if not device:
            raise ValueError("device '%s' could not be found in storage config"
                             % device_id)
        device_paths.append(get_path_to_storage_volume(device_id,
                            storage_config))

    # Handle preserve flag
    if info.get('preserve'):
        # LVM will probably be offline, so start it
        util.subp(["vgchange", "-a", "y"])
        # Verify that volgroup exists and contains all specified devices
        current_paths = []
        (out, _err) = util.subp(["pvdisplay", "-C", "--separator", "=", "-o",
                                "vg_name,pv_name", "--noheadings"],
                                capture=True)
        for line in out.splitlines():
            if name in line:
                current_paths.append(line.split("=")[-1])
        if set(current_paths) != set(device_paths):
            raise ValueError("volgroup '%s' marked to be preserved, but does \
                             not exist or does not contain the right physical \
                             volumes" % info.get('id'))
    else:
        # Create vgrcreate command and run
        cmd = ["vgcreate", name]
        cmd.extend(device_paths)
        util.subp(cmd)


def lvm_partition_handler(info, storage_config):
    volgroup = storage_config.get(info.get('volgroup')).get('name')
    name = info.get('name')
    if not volgroup:
        raise ValueError("lvm volgroup for lvm partition must be specified")
    if not name:
        raise ValueError("lvm partition name must be specified")

    # Handle preserve flag
    if info.get('preserve'):
        (out, _err) = util.subp(["lvdisplay", "-C", "--separator", "=", "-o",
                                "lv_name,vg_name", "--noheadings"],
                                capture=True)
        found = False
        for line in out.splitlines():
            if name in line:
                if volgroup == line.split("=")[-1]:
                    found = True
                    break
        if not found:
            raise ValueError("lvm partition '%s' marked to be preserved, but \
                             does not exist or does not mach storage \
                             configuration" % info.get('id'))
    elif storage_config.get(info.get('volgroup')).get('preserve'):
        raise NotImplementedError("Lvm Partition '%s' is not marked to be \
            preserved, but volgroup '%s' is. At this time, preserving \
            volgroups but not also the lvm partitions on the volgroup is \
            not supported, because of the possibility of damaging lvm \
            partitions intended to be preserved." % (info.get('id'), volgroup))
    else:
        cmd = ["lvcreate", volgroup, "-n", name]
        if info.get('size'):
            cmd.extend(["-L", info.get('size')])
        else:
            cmd.extend(["-l", "100%FREE"])

        util.subp(cmd)

    if info.get('ptable'):
        raise ValueError("Partition tables on top of lvm logical volumes is \
                         not supported")

    make_dname(info.get('id'), storage_config)


def dm_crypt_handler(info, storage_config):
    state = util.load_command_environment()
    volume = info.get('volume')
    key = info.get('key')
    keysize = info.get('keysize')
    cipher = info.get('cipher')
    dm_name = info.get('dm_name')
    if not volume:
        raise ValueError("volume for cryptsetup to operate on must be \
            specified")
    if not key:
        raise ValueError("encryption key must be specified")
    if not dm_name:
        dm_name = info.get('id')

    volume_path = get_path_to_storage_volume(volume, storage_config)

    # TODO: this is insecure, find better way to do this
    tmp_keyfile = tempfile.mkstemp()[1]
    fp = open(tmp_keyfile, "w")
    fp.write(key)
    fp.close()

    cmd = ["cryptsetup"]
    if cipher:
        cmd.extend(["--cipher", cipher])
    if keysize:
        cmd.extend(["--key-size", keysize])
    cmd.extend(["luksFormat", volume_path, tmp_keyfile])

    util.subp(cmd)

    cmd = ["cryptsetup", "open", "--type", "luks", volume_path, dm_name,
           "--key-file", tmp_keyfile]

    util.subp(cmd)

    os.remove(tmp_keyfile)

    # A crypttab will be created in the same directory as the fstab in the
    # configuration. This will then be copied onto the system later
    if state['fstab']:
        crypt_tab_location = os.path.join(os.path.split(state['fstab'])[0],
                                          "crypttab")
        uuid = block.get_volume_uuid(volume_path)
        with open(crypt_tab_location, "a") as fp:
            fp.write("%s UUID=%s none luks\n" % (dm_name, uuid))
    else:
        LOG.info("fstab configuration is not present in environment, so \
            cannot locate an appropriate directory to write crypttab in \
            so not writing crypttab")


def raid_handler(info, storage_config):
    state = util.load_command_environment()
    devices = info.get('devices')
    raidlevel = info.get('raidlevel')
    spare_devices = info.get('spare_devices')
    md_devname = block.dev_path(info.get('name'))
    if not devices:
        raise ValueError("devices for raid must be specified")
    if raidlevel not in ['linear', 'raid0', 0, 'stripe', 'raid1', 1, 'mirror',
                         'raid4', 4, 'raid5', 5, 'raid6', 6, 'raid10', 10]:
        raise ValueError("invalid raidlevel '%s'" % raidlevel)
    if raidlevel in ['linear', 'raid0', 0, 'stripe']:
        if spare_devices:
            raise ValueError("spareunsupported in raidlevel '%s'" % raidlevel)

    LOG.debug('raid: cfg: {}'.format(util.json_dumps(info)))
    device_paths = list(get_path_to_storage_volume(dev, storage_config) for
                        dev in devices)
    LOG.debug('raid: device path mapping: {}'.format(
              zip(devices, device_paths)))

    spare_device_paths = []
    if spare_devices:
        spare_device_paths = list(get_path_to_storage_volume(dev,
                                  storage_config) for dev in spare_devices)
        LOG.debug('raid: spare device path mapping: {}'.format(
                  zip(spare_devices, spare_device_paths)))

    # Handle preserve flag
    if info.get('preserve'):
        # check if the array is already up, if not try to assemble
        if not mdadm.md_check(md_devname, raidlevel,
                              device_paths, spare_device_paths):
            LOG.info("assembling preserved raid for "
                     "{}".format(md_devname))

            mdadm.mdadm_assemble(md_devname, device_paths, spare_device_paths)

            # try again after attempting to assemble
            if not mdadm.md_check(md_devname, raidlevel,
                                  devices, spare_device_paths):
                raise ValueError("Unable to confirm preserved raid array: "
                                 " {}".format(md_devname))
        # raid is all OK
        return

    mdadm.mdadm_create(md_devname, raidlevel,
                       device_paths, spare_device_paths,
                       info.get('mdname', ''))

    # Make dname rule for this dev
    make_dname(info.get('id'), storage_config)

    # A mdadm.conf will be created in the same directory as the fstab in the
    # configuration. This will then be copied onto the installed system later.
    # The file must also be written onto the running system to enable it to run
    # mdadm --assemble and continue installation
    if state['fstab']:
        mdadm_location = os.path.join(os.path.split(state['fstab'])[0],
                                      "mdadm.conf")
        mdadm_scan_data = mdadm.mdadm_detail_scan()
        with open(mdadm_location, "w") as fp:
            fp.write(mdadm_scan_data)
    else:
        LOG.info("fstab configuration is not present in the environment, so \
            cannot locate an appropriate directory to write mdadm.conf in, \
            so not writing mdadm.conf")

    # If ptable is specified, call disk_handler on this mdadm device to create
    # the table
    if info.get('ptable'):
        disk_handler(info, storage_config)


def bcache_handler(info, storage_config):
    backing_device = get_path_to_storage_volume(info.get('backing_device'),
                                                storage_config)
    cache_device = get_path_to_storage_volume(info.get('cache_device'),
                                              storage_config)
    cache_mode = info.get('cache_mode', None)

    if not backing_device or not cache_device:
        raise ValueError("backing device and cache device for bcache"
                         " must be specified")

    # The bcache module is not loaded when bcache is installed by apt-get, so
    # we will load it now
    util.subp(["modprobe", "bcache"])
    bcache_sysfs = "/sys/fs/bcache"
    udevadm_settle(exists=bcache_sysfs)

    def register_bcache(bcache_device):
        with open("/sys/fs/bcache/register", "w") as fp:
            fp.write(bcache_device)

    def ensure_bcache_is_registered(bcache_device, expected, retry=0):
        # find the actual bcache device name via sysfs using the
        # backing device's holders directory.
        LOG.debug('check just created bcache %s if it is registered',
                  bcache_device)
        try:
            udevadm_settle(exists=expected)
            if os.path.exists(expected):
                LOG.debug('Found bcache dev %s at expected path %s',
                          bcache_device, expected)
                return
            LOG.debug('bcache device path not found: %s', expected)
            (local_holders, _err) = clear_holders.get_holders(bcache_device)
            LOG.debug('got initial holders being "%s"', local_holders)
            if len(local_holders) == 0:
                for e in _err:
                    LOG.error('get_holders encountered error: {}'.format(e))
                raise ValueError("holders == 0 , expected non-zero")
        except (OSError, IndexError, ValueError):
            # Some versions of bcache-tools will register the bcache device as
            # soon as we run make-bcache using udev rules, so wait for udev to
            # settle, then try to locate the dev, on older versions we need to
            # register it manually though
            LOG.debug('bcache device was not registered, registering %s at '
                      '/sys/fs/bcache/register', bcache_device)
            try:
                register_bcache(bcache_device)
                udevadm_settle(exists=expected)
            except (IOError):
                # device creation is notoriously racy and this can trigger
                # "Invalid argument" IOErrors if it got created in "the
                # meantime" - just restart the function a few times to
                # check it all again
                if retry < 5:
                    ensure_bcache_is_registered(bcache_device,
                                                expected, (retry+1))
                else:
                    LOG.debug('Repetive error registering the bcache dev %s',
                              bcache_device)
                    raise ValueError("bcache device %s can't be registered",
                                     bcache_device)

    if cache_device:
        # /sys/class/block/XXX/YYY/
        cache_device_sysfs = block.sys_block_path(cache_device)

        if os.path.exists(os.path.join(cache_device_sysfs, "bcache")):
            LOG.debug('caching device already exists at {}/bcache. Read '
                      'cset.uuid'.format(cache_device_sysfs))
            (out, err) = util.subp(["bcache-super-show", cache_device],
                                   capture=True)
            LOG.debug('bcache-super-show=[{}]'.format(out))
            [cset_uuid] = [line.split()[-1] for line in out.split("\n")
                           if line.startswith('cset.uuid')]

        else:
            LOG.debug('caching device does not yet exist at {}/bcache. Make '
                      'cache and get uuid'.format(cache_device_sysfs))
            # make the cache device, extracting cacheset uuid
            (out, err) = util.subp(["make-bcache", "-C", cache_device],
                                   capture=True)
            LOG.debug('out=[{}]'.format(out))
            [cset_uuid] = [line.split()[-1] for line in out.split("\n")
                           if line.startswith('Set UUID:')]

        target_sysfs_path = '/sys/fs/bcache/%s' % cset_uuid
        ensure_bcache_is_registered(cache_device, target_sysfs_path)

    if backing_device:
        backing_device_sysfs = block.sys_block_path(backing_device)
        target_sysfs_path = os.path.join(backing_device_sysfs, "bcache")
        if not os.path.exists(os.path.join(backing_device_sysfs, "bcache")):
            util.subp(["make-bcache", "-B", backing_device])
        ensure_bcache_is_registered(backing_device, target_sysfs_path)

        # via the holders we can identify which bcache device we just created
        # for a given backing device
        (holders, _err) = clear_holders.get_holders(backing_device)
        if len(holders) != 1:
            for e in _err:
                LOG.error('get_holders encountered error: {}'.format(e))
            err = ('Invalid number {} of holding devices:'
                   ' "{}"'.format(len(holders), holders))
            LOG.error(err)
            raise ValueError(err)
        [bcache_dev] = holders
        LOG.debug('The just created bcache device is {}'.format(holders))

        if cache_device:
            # if we specify both then we need to attach backing to cache
            if cset_uuid:
                LOG.info("Attaching backing device to cacheset: "
                         "{} -> {} cset.uuid: {}".format(backing_device,
                                                         cache_device,
                                                         cset_uuid))
                attach = os.path.join(backing_device_sysfs,
                                      "bcache",
                                      "attach")
                with open(attach, "w") as fp:
                    fp.write(cset_uuid)
            else:
                msg = "Invalid cset_uuid: {}".format(cset_uuid)
                LOG.error(msg)
                raise ValueError(msg)

        if cache_mode:
            LOG.info("Setting cache_mode on {} to {}".format(bcache_dev,
                                                             cache_mode))
            cache_mode_file = \
                '/sys/block/{}/bcache/cache_mode'.format(bcache_dev)
            with open(cache_mode_file, "w") as fp:
                fp.write(cache_mode)
    else:
        # no backing device
        if cache_mode:
            raise ValueError("cache mode specified which can only be set per \
                              backing devices, but none was specified")

    if info.get('name'):
        # Make dname rule for this dev
        make_dname(info.get('id'), storage_config)

    if info.get('ptable'):
        raise ValueError("Partition tables on top of lvm logical volumes is \
                         not supported")
    LOG.debug('Finished bcache creation for backing {} or caching {}'
              .format(backing_device, cache_device))


def extract_storage_ordered_dict(config):
    storage_config = config.get('storage', {})
    if not storage_config:
        raise ValueError("no 'storage' entry in config")
    scfg = storage_config.get('config')
    if not scfg:
        raise ValueError("invalid storage config data")

    # Since storage config will often have to be searched for a value by its
    # id, and this can become very inefficient as storage_config grows, a dict
    # will be generated with the id of each component of the storage_config as
    # its index and the component of storage_config as its value
    return OrderedDict((d["id"], d) for (i, d) in enumerate(scfg))


def meta_custom(args):
    """Does custom partitioning based on the layout provided in the config
    file. Section with the name storage contains information on which
    partitions on which disks to create. It also contains information about
    overlays (raid, lvm, bcache) which need to be setup.
    """

    command_handlers = {
        'disk': disk_handler,
        'partition': partition_handler,
        'format': format_handler,
        'mount': mount_handler,
        'lvm_volgroup': lvm_volgroup_handler,
        'lvm_partition': lvm_partition_handler,
        'dm_crypt': dm_crypt_handler,
        'raid': raid_handler,
        'bcache': bcache_handler
    }

    state = util.load_command_environment()
    cfg = config.load_command_config(args, state)

    storage_config_dict = extract_storage_ordered_dict(cfg)

    # set up reportstack
    stack_prefix = state.get('report_stack_prefix', '')

    for item_id, command in storage_config_dict.items():
        handler = command_handlers.get(command['type'])
        if not handler:
            raise ValueError("unknown command type '%s'" % command['type'])
        with events.ReportEventStack(
                name=stack_prefix, reporting_enabled=True, level="INFO",
                description="configuring %s: %s" % (command['type'],
                                                    command['id'])):
            try:
                handler(command, storage_config_dict)
            except Exception as error:
                LOG.error("An error occured handling '%s': %s - %s" %
                          (item_id, type(error).__name__, error))
                raise

    return 0


def meta_simple(args):
    """Creates a root partition. If args.mode == SIMPLE_BOOT, it will also
    create a separate /boot partition.
    """
    state = util.load_command_environment()

    cfg = config.load_command_config(args, state)

    if args.target is not None:
        state['target'] = args.target

    if state['target'] is None:
        sys.stderr.write("Unable to find target.  "
                         "Use --target or set TARGET_MOUNT_POINT\n")
        sys.exit(2)

    devices = args.devices
    if devices is None:
        devices = cfg.get('block-meta', {}).get('devices', [])

    bootpt = get_bootpt_cfg(
        cfg.get('block-meta', {}).get('boot-partition', {}),
        enabled=args.mode == SIMPLE_BOOT, fstype=args.boot_fstype,
        root_fstype=args.fstype)

    ptfmt = get_partition_format_type(cfg.get('block-meta', {}))

    # Remove duplicates but maintain ordering.
    devices = list(OrderedDict.fromkeys(devices))

    # Multipath devices might be automatically assembled if multipath-tools
    # package is available in the installation environment. We need to stop
    # all multipath devices to exclusively use one of paths as a target disk.
    block.stop_all_unused_multipath_devices()

    if len(devices) == 0:
        devices = block.get_installable_blockdevs()
        LOG.warn("'%s' mode, no devices given. unused list: %s",
                 args.mode, devices)
        # Check if the list of installable block devices is still empty after
        # checking for block devices and filtering out the removable ones.  In
        # this case we may have a system which has its harddrives reported by
        # lsblk incorrectly. In this case we search for installable
        # blockdevices that are removable as a last resort before raising an
        # exception.
        if len(devices) == 0:
            devices = block.get_installable_blockdevs(include_removable=True)
            if len(devices) == 0:
                # Fail gracefully if no devices are found, still.
                raise Exception("No valid target devices found that curtin "
                                "can install on.")
            else:
                LOG.warn("No non-removable, installable devices found. List "
                         "populated with removable devices allowed: %s",
                         devices)

    if len(devices) > 1:
        if args.devices is not None:
            LOG.warn("'%s' mode but multiple devices given. "
                     "using first found", args.mode)
        available = [f for f in devices
                     if block.is_valid_device(f)]
        target = sorted(available)[0]
        LOG.warn("mode is '%s'. multiple devices given. using '%s' "
                 "(first available)", args.mode, target)
    else:
        target = devices[0]

    if not block.is_valid_device(target):
        raise Exception("target device '%s' is not a valid device" % target)

    (devname, devnode) = block.get_dev_name_entry(target)

    LOG.info("installing in '%s' mode to '%s'", args.mode, devname)

    sources = cfg.get('sources', {})
    dd_images = util.get_dd_images(sources)

    if len(dd_images):
        # we have at least one dd-able image
        # we will only take the first one
        rootdev = write_image_to_disk(dd_images[0], devname)
        util.subp(['mount', rootdev, state['target']])
        return 0

    # helper partition will forcibly set up partition there
    ptcmd = ['partition', '--format=' + ptfmt]
    if bootpt['enabled']:
        ptcmd.append('--boot')
    ptcmd.append(devnode)

    if bootpt['enabled'] and ptfmt in ("uefi", "prep"):
        raise ValueError("format=%s with boot partition not supported" % ptfmt)

    bootdev_ptnum = None
    rootdev_ptnum = None
    bootdev = None
    if bootpt['enabled']:
        bootdev_ptnum = 1
        rootdev_ptnum = 2
    else:
        if ptfmt == "prep":
            rootdev_ptnum = 2
        else:
            rootdev_ptnum = 1

    logtime("creating partition with: %s" % ' '.join(ptcmd),
            util.subp, ptcmd)

    ptpre = ""
    if not os.path.exists("%s%s" % (devnode, rootdev_ptnum)):
        # perhaps the device is /dev/<blockname>p<ptnum>
        if os.path.exists("%sp%s" % (devnode, rootdev_ptnum)):
            ptpre = "p"
        else:
            LOG.warn("root device %s%s did not exist, expecting failure",
                     devnode, rootdev_ptnum)

    if bootdev_ptnum:
        bootdev = "%s%s%s" % (devnode, ptpre, bootdev_ptnum)

    if ptfmt == "uefi":
        # assumed / required from the partitioner pt_uefi
        uefi_ptnum = "15"
        uefi_label = "uefi-boot"
        uefi_dev = "%s%s%s" % (devnode, ptpre, uefi_ptnum)

    rootdev = "%s%s%s" % (devnode, ptpre, rootdev_ptnum)

    LOG.debug("rootdev=%s bootdev=%s fmt=%s bootpt=%s",
              rootdev, bootdev, ptfmt, bootpt)

    # mkfs for root partition first and mount
    cmd = ['mkfs.%s' % args.fstype, '-q', '-L', 'cloudimg-rootfs', rootdev]
    logtime(' '.join(cmd), util.subp, cmd)
    util.subp(['mount', rootdev, state['target']])

    if bootpt['enabled']:
        # create 'boot' directory in state['target']
        boot_dir = os.path.join(state['target'], 'boot')
        util.subp(['mkdir', boot_dir])
        # mkfs for boot partition and mount
        cmd = ['mkfs.%s' % bootpt['fstype'],
               '-q', '-L', bootpt['label'], bootdev]
        logtime(' '.join(cmd), util.subp, cmd)
        util.subp(['mount', bootdev, boot_dir])

    if ptfmt == "uefi":
        uefi_dir = os.path.join(state['target'], 'boot', 'efi')
        util.ensure_dir(uefi_dir)
        util.subp(['mount', uefi_dev, uefi_dir])

    if state['fstab']:
        with open(state['fstab'], "w") as fp:
            if bootpt['enabled']:
                fp.write("LABEL=%s /boot %s defaults 0 0\n" %
                         (bootpt['label'], bootpt['fstype']))

            if ptfmt == "uefi":
                # label created in helpers/partition for uefi
                fp.write("LABEL=%s /boot/efi vfat defaults 0 0\n" %
                         uefi_label)

            fp.write("LABEL=%s / %s defaults 0 0\n" %
                     ('cloudimg-rootfs', args.fstype))
    else:
        LOG.info("fstab not in environment, so not writing")

    return 0


def POPULATE_SUBCMD(parser):
    populate_one_subcmd(parser, CMD_ARGUMENTS, block_meta)

# vi: ts=4 expandtab syntax=python
