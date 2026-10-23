"""Negative compiled-policy fixtures against the actual APK surface validator."""
import importlib.util
from pathlib import Path
import unittest
from surface_fixture import PERMISSIONS, XML

spec=importlib.util.spec_from_file_location('foreground_surface_binding',Path(__file__).resolve().parents[1]/'artifact-binding.py')
binding=importlib.util.module_from_spec(spec);spec.loader.exec_module(binding)
BADGING="package: name='ai.lastcore.android' versionCode='1' versionName='0.1.0-draft'\nsdkVersion:'29'\ntargetSdkVersion:'35'\n"+PERMISSIONS

class CompiledSurfaceTests(unittest.TestCase):
    def test_exact_surface_and_both_class_name_forms(self):
        binding.verify_surface(BADGING,XML)
        binding.verify_surface(BADGING,XML.replace('ai.lastcore.android.CloudWorkerService','.CloudWorkerService').replace('ai.lastcore.android.MainActivity','.MainActivity'))
    def test_missing_extra_and_duplicate_permissions_rejected(self):
        for badging in [BADGING.replace("uses-permission: name='android.permission.POST_NOTIFICATIONS'\n",''),
                        BADGING+"uses-permission: name='android.permission.WAKE_LOCK'\n",BADGING+PERMISSIONS.splitlines()[0]+'\n',
                        BADGING+"uses-permission-sdk-23: name='android.permission.WAKE_LOCK'\n"]:
            with self.subTest(badging=badging),self.assertRaises(SystemExit):binding.verify_surface(badging,XML)
    def test_extra_component_and_process_surfaces_rejected(self):
        for addition in ('    E: receiver\n','    E: provider\n','    E: activity-alias\n','    E: service\n',
                         '    A: android:process(0x01010011)="other"\n'):
            with self.subTest(addition=addition),self.assertRaises(SystemExit):binding.verify_surface(BADGING,XML+addition)
    def test_exported_foreign_or_mistyped_service_rejected(self):
        for source in [XML.replace('CloudWorkerService','ForeignService'),
                       XML.replace('android:exported(0x01010010)=(type 0x12)0x0','android:exported(0x01010010)=(type 0x12)0xffffffff'),
                       XML.replace('0x40000000','0x40000001'),XML.replace('0x40000000','0x0')]:
            with self.subTest(source=source),self.assertRaises(SystemExit):binding.verify_surface(BADGING,source)
    def test_purpose_backup_cleartext_package_and_sdk_rejected(self):
        for source in [XML.replace('PROPERTY_SPECIAL_USE_FGS_SUBTYPE','OTHER_PROPERTY'),
                       XML.replace('User-started visible','Unreviewed hidden'),
                       XML.replace('android:allowBackup(0x01010280)=(type 0x12)0x0','android:allowBackup(0x01010280)=(type 0x12)0xffffffff'),
                       XML.replace('android:usesCleartextTraffic(0x010104ec)=(type 0x12)0x0','android:usesCleartextTraffic(0x010104ec)=(type 0x12)0xffffffff')]:
            with self.subTest(source=source),self.assertRaises(SystemExit):binding.verify_surface(BADGING,source)
        for badging in [BADGING.replace("name='ai.lastcore.android'","name='other.app'"),BADGING.replace("targetSdkVersion:'35'","targetSdkVersion:'36'")]:
            with self.subTest(badging=badging),self.assertRaises(SystemExit):binding.verify_surface(badging,XML)
    def test_other_node_attributes_cannot_satisfy_service_policy(self):
        missing=XML.replace('      A: android:exported(0x01010010)=(type 0x12)0x0\n','')
        with self.assertRaises(SystemExit):binding.verify_surface(BADGING,missing)
    def test_isolation_and_zygote_flags_allow_only_absent_or_explicit_false(self):
        for flag in ('isolatedProcess','useAppZygote','externalService','allowSharedIsolatedProcess'):
            prefix='      A: android:'+flag+'(0x010103a9)='
            false=XML.replace('    E: activity',prefix+'(type 0x12)0x0\n    E: activity')
            binding.verify_surface(BADGING,false)
            for value in ('(type 0x12)0xffffffff','(type 0x12)0x1','(type 0x10)0x0','"false"'):
                source=XML.replace('    E: activity',prefix+value+'\n    E: activity')
                with self.subTest(flag=flag,value=value),self.assertRaises(SystemExit):binding.verify_surface(BADGING,source)
        with self.assertRaises(SystemExit):
            binding.verify_surface(BADGING,XML.replace('    E: activity','      A: android:zygotePreloadName(0x01010003)="unexpected"\n    E: activity'))

if __name__=='__main__':unittest.main()
