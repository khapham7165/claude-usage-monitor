# -*- mode: python ; coding: utf-8 -*-
import os, certifi

block_cipher = None

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
        (os.path.dirname(certifi.where()), 'certifi'),
    ],
    hiddenimports=[
        'backend',
        'backend.aggregators',
        'backend.active_sessions',
        'backend.auth',
        'backend.claude_web',
        'backend.cost_model',
        'backend.cursor',
        'backend.parsers',
        'backend.ssh_collector',
        'cloudscraper',
        'paramiko',
        'flask',
        'webview',
        'webview.platforms',
        'webview.platforms.cocoa',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Claude Usage Monitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Claude Usage Monitor',
)

app = BUNDLE(
    coll,
    name='Claude Usage Monitor.app',
    icon=None,
    bundle_identifier='com.khapham.claude-usage-monitor',
    info_plist={
        'NSHighResolutionCapable': True,
    },
)
