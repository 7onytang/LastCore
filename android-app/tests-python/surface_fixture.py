"""Explicit synthetic compiled-manifest fixture; no SDK, credentials or APK execution."""
PERMISSIONS = """uses-permission: name='android.permission.INTERNET'
uses-permission: name='android.permission.FOREGROUND_SERVICE'
uses-permission: name='android.permission.FOREGROUND_SERVICE_SPECIAL_USE'
uses-permission: name='android.permission.POST_NOTIFICATIONS'
"""
XML = '''E: manifest
  E: application
    A: android:allowBackup(0x01010280)=(type 0x12)0x0
    A: android:usesCleartextTraffic(0x010104ec)=(type 0x12)0x0
    E: service
      A: android:name(0x01010003)="ai.lastcore.android.CloudWorkerService"
      A: android:exported(0x01010010)=(type 0x12)0x0
      A: android:foregroundServiceType(0x01010599)=(type 0x11)0x40000000
      E: property
        A: android:name(0x01010003)="android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE"
        A: android:value(0x01010024)="User-started visible on-device CPU whole-model worker with bounded assigned tasks and explicit Stop; no automatic restart."
    E: activity
      A: android:name(0x01010003)="ai.lastcore.android.MainActivity"
      A: android:exported(0x01010010)=(type 0x12)0xffffffff
'''
