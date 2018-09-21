# This file is part of curtin. See LICENSE file for copyright and license info.

from unittest import skipIf
import mock
import sys

from curtin import distro
from curtin import paths
from curtin import util
from .helpers import CiTestCase


class TestLsbRelease(CiTestCase):

    def setUp(self):
        super(TestLsbRelease, self).setUp()
        self._reset_cache()

    def _reset_cache(self):
        keys = [k for k in distro._LSB_RELEASE.keys()]
        for d in keys:
            del distro._LSB_RELEASE[d]

    @mock.patch("curtin.distro.subp")
    def test_lsb_release_functional(self, mock_subp):
        output = '\n'.join([
            "Distributor ID: Ubuntu",
            "Description:    Ubuntu 14.04.2 LTS",
            "Release:    14.04",
            "Codename:   trusty",
        ])
        rdata = {'id': 'Ubuntu', 'description': 'Ubuntu 14.04.2 LTS',
                 'codename': 'trusty', 'release': '14.04'}

        def fake_subp(cmd, capture=False, target=None):
            return output, 'No LSB modules are available.'

        mock_subp.side_effect = fake_subp
        found = distro.lsb_release()
        mock_subp.assert_called_with(
            ['lsb_release', '--all'], capture=True, target=None)
        self.assertEqual(found, rdata)

    @mock.patch("curtin.distro.subp")
    def test_lsb_release_unavailable(self, mock_subp):
        def doraise(*args, **kwargs):
            raise util.ProcessExecutionError("foo")
        mock_subp.side_effect = doraise

        expected = {k: "UNAVAILABLE" for k in
                    ('id', 'description', 'codename', 'release')}
        self.assertEqual(distro.lsb_release(), expected)


class TestParseDpkgVersion(CiTestCase):
    """test parse_dpkg_version."""

    def test_none_raises_type_error(self):
        self.assertRaises(TypeError, distro.parse_dpkg_version, None)

    @skipIf(sys.version_info.major < 3, "python 2 bytes are strings.")
    def test_bytes_raises_type_error(self):
        self.assertRaises(TypeError, distro.parse_dpkg_version, b'1.2.3-0')

    def test_simple_native_package_version(self):
        """dpkg versions must have a -. If not present expect value error."""
        self.assertEqual(
            {'major': 2, 'minor': 28, 'micro': 0, 'extra': None,
             'raw': '2.28', 'upstream': '2.28', 'name': 'germinate',
             'semantic_version': 22800},
            distro.parse_dpkg_version('2.28', name='germinate'))

    def test_complex_native_package_version(self):
        dver = '1.0.106ubuntu2+really1.0.97ubuntu1'
        self.assertEqual(
            {'major': 1, 'minor': 0, 'micro': 106,
             'extra': 'ubuntu2+really1.0.97ubuntu1',
             'raw': dver, 'upstream': dver, 'name': 'debootstrap',
             'semantic_version': 100106},
            distro.parse_dpkg_version(dver, name='debootstrap',
                                      semx=(100000, 1000, 1)))

    def test_simple_valid(self):
        self.assertEqual(
            {'major': 1, 'minor': 2, 'micro': 3, 'extra': None,
             'raw': '1.2.3-0', 'upstream': '1.2.3', 'name': 'foo',
             'semantic_version': 10203},
            distro.parse_dpkg_version('1.2.3-0', name='foo'))

    def test_simple_valid_with_semx(self):
        self.assertEqual(
            {'major': 1, 'minor': 2, 'micro': 3, 'extra': None,
             'raw': '1.2.3-0', 'upstream': '1.2.3',
             'semantic_version': 123},
            distro.parse_dpkg_version('1.2.3-0', semx=(100, 10, 1)))

    def test_upstream_with_hyphen(self):
        """upstream versions may have a hyphen."""
        cver = '18.2-14-g6d48d265-0ubuntu1'
        self.assertEqual(
            {'major': 18, 'minor': 2, 'micro': 0, 'extra': '-14-g6d48d265',
             'raw': cver, 'upstream': '18.2-14-g6d48d265',
             'name': 'cloud-init', 'semantic_version': 180200},
            distro.parse_dpkg_version(cver, name='cloud-init'))

    def test_upstream_with_plus(self):
        """multipath tools has a + in it."""
        mver = '0.5.0+git1.656f8865-5ubuntu2.5'
        self.assertEqual(
            {'major': 0, 'minor': 5, 'micro': 0, 'extra': '+git1.656f8865',
             'raw': mver, 'upstream': '0.5.0+git1.656f8865',
             'semantic_version': 500},
            distro.parse_dpkg_version(mver))


class TestDistros(CiTestCase):

    def test_distro_names(self):
        all_distros = list(distro.DISTROS)
        for distro_name in distro.DISTRO_NAMES:
            distro_enum = getattr(distro.DISTROS, distro_name)
            self.assertIn(distro_enum, all_distros)

    def test_distro_names_unknown(self):
        distro_name = "ImNotADistro"
        self.assertNotIn(distro_name, distro.DISTRO_NAMES)
        with self.assertRaises(AttributeError):
            getattr(distro.DISTROS, distro_name)

    def test_distro_osfamily(self):
        for variant, family in distro.OS_FAMILIES.items():
            self.assertNotEqual(variant, family)
            self.assertIn(variant, distro.DISTROS)
            for dname in family:
                self.assertIn(dname, distro.DISTROS)

    def test_distro_osfmaily_identity(self):
        for family, variants in distro.OS_FAMILIES.items():
            self.assertIn(family, variants)

    def test_name_to_distro(self):
        for distro_name in distro.DISTRO_NAMES:
            dobj = distro.name_to_distro(distro_name)
            self.assertEqual(dobj, getattr(distro.DISTROS, distro_name))

    def test_name_to_distro_unknown_value(self):
        with self.assertRaises(ValueError):
            distro.name_to_distro(None)

    def test_name_to_distro_unknown_attr(self):
        with self.assertRaises(ValueError):
            distro.name_to_distro('NotADistro')

    def test_distros_unknown_attr(self):
        with self.assertRaises(AttributeError):
            distro.DISTROS.notadistro

    def test_distros_unknown_index(self):
        with self.assertRaises(IndexError):
            distro.DISTROS[len(distro.DISTROS)+1]


class TestDistroInfo(CiTestCase):

    def setUp(self):
        super(TestDistroInfo, self).setUp()
        self.add_patch('curtin.distro.os_release', 'mock_os_release')

    def test_get_distroinfo(self):
        for distro_name in distro.DISTRO_NAMES:
            self.mock_os_release.return_value = {'ID': distro_name}
            variant = distro.name_to_distro(distro_name)
            family = distro.DISTRO_TO_OSFAMILY[variant]
            distro_info = distro.get_distroinfo()
            self.assertEqual(variant, distro_info.variant)
            self.assertEqual(family, distro_info.family)

    def test_get_distro(self):
        for distro_name in distro.DISTRO_NAMES:
            self.mock_os_release.return_value = {'ID': distro_name}
            variant = distro.name_to_distro(distro_name)
            distro_obj = distro.get_distro()
            self.assertEqual(variant, distro_obj)

    def test_get_osfamily(self):
        for distro_name in distro.DISTRO_NAMES:
            self.mock_os_release.return_value = {'ID': distro_name}
            variant = distro.name_to_distro(distro_name)
            family = distro.DISTRO_TO_OSFAMILY[variant]
            distro_obj = distro.get_osfamily()
            self.assertEqual(family, distro_obj)


class TestDistroIdentity(CiTestCase):

    def setUp(self):
        super(TestDistroIdentity, self).setUp()
        self.add_patch('curtin.distro.os.path.exists', 'mock_os_path')

    def test_is_ubuntu_core(self):
        for exists in [True, False]:
            self.mock_os_path.return_value = exists
            self.assertEqual(exists, distro.is_ubuntu_core())
            self.mock_os_path.assert_called_with('/system-data/var/lib/snapd')

    def test_is_centos(self):
        for exists in [True, False]:
            self.mock_os_path.return_value = exists
            self.assertEqual(exists, distro.is_centos())
            self.mock_os_path.assert_called_with('/etc/centos-release')

    def test_is_rhel(self):
        for exists in [True, False]:
            self.mock_os_path.return_value = exists
            self.assertEqual(exists, distro.is_rhel())
            self.mock_os_path.assert_called_with('/etc/redhat-release')


class TestYumInstall(CiTestCase):

    @mock.patch.object(util.ChrootableTarget, "__enter__", new=lambda a: a)
    @mock.patch('curtin.util.subp')
    def test_yum_install(self, m_subp):
        pkglist = ['foobar', 'wark']
        target = 'mytarget'
        mode = 'install'
        expected_calls = [
            mock.call(['yum', '--assumeyes', '--quiet', 'install',
                       '--downloadonly', '--setopt=keepcache=1'] + pkglist,
                      env=None, retries=[1] * 10,
                      target=paths.target_path(target)),
            mock.call(['yum', '--assumeyes', '--quiet', 'install',
                       '--cacheonly'] + pkglist, env=None,
                      target=paths.target_path(target))
        ]

        # call yum_install directly
        distro.yum_install(mode, pkglist, target=target)
        m_subp.assert_has_calls(expected_calls)

        # call yum_install through run_yum_command
        m_subp.reset()
        distro.run_yum_command('install', pkglist, target=target)
        m_subp.assert_has_calls(expected_calls)

        # call yum_install through install_packages
        m_subp.reset()
        osfamily = distro.DISTROS.redhat
        distro.install_packages(pkglist, osfamily=osfamily, target=target)
        m_subp.assert_has_calls(expected_calls)


class TestHasPkgAvailable(CiTestCase):

    def setUp(self):
        super(TestHasPkgAvailable, self).setUp()
        self.package = 'foobar'
        self.target = paths.target_path('mytarget')

    @mock.patch.object(util.ChrootableTarget, "__enter__", new=lambda a: a)
    @mock.patch('curtin.distro.subp')
    def test_has_pkg_available_debian(self, m_subp):
        osfamily = distro.DISTROS.debian
        m_subp.return_value = (self.package, '')
        result = distro.has_pkg_available(self.package, self.target, osfamily)
        self.assertTrue(result)
        m_subp.assert_has_calls([mock.call(['apt-cache', 'pkgnames'],
                                           capture=True,
                                           target=self.target)])

    @mock.patch.object(util.ChrootableTarget, "__enter__", new=lambda a: a)
    @mock.patch('curtin.distro.subp')
    def test_has_pkg_available_debian_returns_false_not_avail(self, m_subp):
        pkg = 'wark'
        osfamily = distro.DISTROS.debian
        m_subp.return_value = (pkg, '')
        result = distro.has_pkg_available(self.package, self.target, osfamily)
        self.assertEqual(pkg == self.package, result)
        m_subp.assert_has_calls([mock.call(['apt-cache', 'pkgnames'],
                                           capture=True,
                                           target=self.target)])

    @mock.patch.object(util.ChrootableTarget, "__enter__", new=lambda a: a)
    @mock.patch('curtin.distro.run_yum_command')
    def test_has_pkg_available_redhat(self, m_subp):
        osfamily = distro.DISTROS.redhat
        m_subp.return_value = (self.package, '')
        result = distro.has_pkg_available(self.package, self.target, osfamily)
        self.assertTrue(result)
        m_subp.assert_has_calls([mock.call('list', opts=['--cacheonly'])])

    @mock.patch.object(util.ChrootableTarget, "__enter__", new=lambda a: a)
    @mock.patch('curtin.distro.run_yum_command')
    def test_has_pkg_available_redhat_returns_false_not_avail(self, m_subp):
        pkg = 'wark'
        osfamily = distro.DISTROS.redhat
        m_subp.return_value = (pkg, '')
        result = distro.has_pkg_available(self.package, self.target, osfamily)
        self.assertEqual(pkg == self.package, result)
        m_subp.assert_has_calls([mock.call('list', opts=['--cacheonly'])])

# vi: ts=4 expandtab syntax=python
