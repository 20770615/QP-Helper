# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for macOS .app / .dmg builds.
# Run from repo root on a Mac:
#   .venv/bin/pyinstaller packaging/macos/qp-helper.spec

import os
from pathlib import Path

block_cipher = None
# SPECPATH is the directory that contains this .spec file.
ROOT = Path(os.path.abspath(SPECPATH)).resolve().parents[1]
SRC = ROOT / "src"

a = Analysis(
    [str(SRC / "qp_email_helper.py")],
    pathex=[str(SRC), str(ROOT)],
    binaries=[],
    datas=[
        (str(SRC / "styles.qss"), "."),
        (str(SRC / "i18n_en.json"), "."),
        (str(SRC / "i18n_zh.json"), "."),
        (str(ROOT / "qp_email_helper.config.example.json"), "."),
        (str(ROOT / "packaging" / "macos" / "Info.plist"), "."),
    ],
    hiddenimports=[
        "platform_compat",
        "config_manager",
        "i18n",
        "rag_client",
        "shared_store",
        "popup_manager",
        "audit_logger",
        "metadata_cache",
        "context_assembler",
        "views_database",
        "views_email",
        "views_prompts",
        "views_upload",
        "PyQt6",
        "httpx",
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
    name="QP Helper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=True,  # macOS: better Finder / Dock double-click handling
    target_arch=None,
    codesign_identity=None,
    entitlements_file=str(ROOT / "packaging" / "macos" / "entitlements.plist"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="QP Helper",
)

app = BUNDLE(
    coll,
    name="QP Helper.app",
    icon=None,  # drop a .icns at packaging/macos/AppIcon.icns and set here
    bundle_identifier="local.quamplus.qp-helper",
    info_plist={
        "CFBundleDisplayName": "QP Helper",
        "CFBundleName": "QP Helper",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    },
)
