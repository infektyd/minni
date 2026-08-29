#!/usr/bin/env python3
"""Write a hand-maintainable Xcode project so the folder opens without XcodeGen."""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def pid(name: str) -> str:
    return hashlib.sha1(name.encode()).hexdigest()[:24].upper()


CORE = [
    "Sources/UsageDockCore/ProviderKind.swift",
    "Sources/UsageDockCore/Utilization.swift",
    "Sources/UsageDockCore/UsageWindow.swift",
    "Sources/UsageDockCore/ProviderSnapshot.swift",
    "Sources/UsageDockCore/ResetClock.swift",
    "Sources/UsageDockCore/ClaudeUsagePayload.swift",
    "Sources/UsageDockCore/FixtureCatalog.swift",
    "Sources/UsageDockCore/DockMetrics.swift",
]

APP = [
    "Sources/UsageDock/Palette.swift",
    "Sources/UsageDock/AppSettings.swift",
    "Sources/UsageDock/UsageStore.swift",
    "Sources/UsageDock/UsageDockApp.swift",
    "Sources/UsageDock/DockController.swift",
    "Sources/UsageDock/Adapters/ProviderAdapter.swift",
    "Sources/UsageDock/Adapters/UnsupportedAdapter.swift",
    "Sources/UsageDock/Adapters/ClaudeCredentials.swift",
    "Sources/UsageDock/Adapters/ClaudeUsageClient.swift",
    "Sources/UsageDock/Adapters/ClaudeAdapter.swift",
    "Sources/UsageDock/Views/ProviderMark.swift",
    "Sources/UsageDock/Views/RingGauge.swift",
    "Sources/UsageDock/Views/UsagePopover.swift",
    "Sources/UsageDock/Views/EdgeDockView.swift",
    "Sources/UsageDock/Views/SettingsView.swift",
    "Sources/UsageDock/Views/MenuBarExtraView.swift",
]

RESOURCES = [
    "Sources/UsageDock/Resources/Assets.xcassets",
]

TEST_ONLY = [
    "Tests/UsageDockCoreTests/UtilizationTests.swift",
    "Tests/UsageDockCoreTests/ResetClockTests.swift",
    "Tests/UsageDockCoreTests/ClaudeUsagePayloadTests.swift",
    "Tests/UsageDockCoreTests/ClaudeCredentialsTests.swift",
    "Sources/UsageDock/Adapters/ClaudeCredentials.swift",
]

PLIST = "Sources/UsageDock/Resources/Info.plist"
ENTITLEMENTS = "Sources/UsageDock/Resources/UsageDock.entitlements"


def file_ref(path: str, last_known: str | None = None) -> str:
    name = Path(path).name
    file_type = {
        ".swift": "sourcecode.swift",
        ".plist": "text.plist.xml",
        ".entitlements": "text.plist.entitlements",
        ".xcassets": "folder.assetcatalog",
        ".json": "text.json",
        ".md": "net.daringfireball.markdown",
        ".yml": "text.yaml",
        ".html": "text.html",
        ".py": "text.script.python",
    }.get(Path(path).suffix, "text")
    extra = f" lastKnownFileType = {last_known or file_type};"
    if path.endswith(".xcassets"):
        extra = " lastKnownFileType = folder.assetcatalog;"
    return (
        f"\t\t{pid('ref:' + path)} /* {name} */ = {{isa = PBXFileReference;{extra} path = {name}; sourceTree = \"<group>\"; }};\n"
    )


def build_file(path: str, prefix: str) -> str:
    name = Path(path).name
    return (
        f"\t\t{pid(prefix + path)} /* {name} in Sources */ = "
        f"{{isa = PBXBuildFile; fileRef = {pid('ref:' + path)} /* {name} */; }};\n"
    )


def group(name: str, children: list[str], path: str | None = None) -> str:
    child_lines = "".join(
        f"\t\t\t\t{pid('ref:' + child)} /* {Path(child).name} */,\n" for child in children
    )
    path_line = f"\t\t\tpath = {path};\n" if path else ""
    return (
        f"\t\t{pid('group:' + name)} /* {name} */ = {{\n"
        f"\t\t\tisa = PBXGroup;\n"
        f"\t\t\tchildren = (\n{child_lines}\t\t\t);\n"
        f"{path_line}"
        f"\t\t\tsourceTree = \"<group>\";\n"
        f"\t\t}};\n"
    )


def sources_phase(phase_id: str, name: str, files: list[str], prefix: str) -> str:
    lines = "".join(
        f"\t\t\t\t{pid(prefix + path)} /* {Path(path).name} in Sources */,\n" for path in files
    )
    return (
        f"\t\t{phase_id} /* {name} */ = {{\n"
        f"\t\t\tisa = PBXSourcesBuildPhase;\n"
        f"\t\t\tbuildActionMask = 2147483647;\n"
        f"\t\t\tfiles = (\n{lines}\t\t\t);\n"
        f"\t\t\trunOnlyForDeploymentPostprocessing = 0;\n"
        f"\t\t}};\n"
    )


def config(name: str, ident: str, extra: dict[str, str]) -> str:
    base = {
        "ALWAYS_SEARCH_USER_PATHS": "NO",
        "CLANG_ENABLE_MODULES": "YES",
        "CLANG_ENABLE_OBJC_ARC": "YES",
        "COPY_PHASE_STRIP": "NO",
        "DEAD_CODE_STRIPPING": "YES",
        "ENABLE_STRICT_OBJC_MSGSEND": "YES",
        "GCC_NO_COMMON_BLOCKS": "YES",
        "MACOSX_DEPLOYMENT_TARGET": "15.0",
        "SDKROOT": "macosx",
        "SWIFT_STRICT_CONCURRENCY": "complete",
        "SWIFT_VERSION": "6.0",
    }
    if name == "Debug":
        base.update(
            {
                "DEBUG_INFORMATION_FORMAT": "dwarf",
                "ENABLE_TESTABILITY": "YES",
                "GCC_OPTIMIZATION_LEVEL": "0",
                "ONLY_ACTIVE_ARCH": "YES",
                "SWIFT_ACTIVE_COMPILATION_CONDITIONS": "DEBUG",
                "SWIFT_OPTIMIZATION_LEVEL": "-Onone",
            }
        )
    else:
        base.update(
            {
                "DEBUG_INFORMATION_FORMAT": "dwarf-with-dsym",
                "SWIFT_COMPILATION_MODE": "wholemodule",
                "SWIFT_OPTIMIZATION_LEVEL": "-O",
            }
        )
    base.update(extra)
    settings = "".join(f"\t\t\t\t{k} = {v};\n" for k, v in base.items())
    return (
        f"\t\t{ident} /* {name} */ = {{\n"
        f"\t\t\tisa = XCBuildConfiguration;\n"
        f"\t\t\tbuildSettings = {{\n{settings}\t\t\t}};\n"
        f"\t\t\tname = {name};\n"
        f"\t\t}};\n"
    )


def main() -> None:
    app_sources = CORE + APP
    test_sources = CORE + TEST_ONLY
    docs = [
        "DESIGN.md",
        "README.md",
        "project.yml",
        "Preview/index.html",
        "tools/check_core.py",
    ]

    build_files = "".join(build_file(path, "app:") for path in app_sources)
    build_files += (
        f"\t\t{pid('appres:assets')} /* Assets.xcassets in Resources */ = "
        f"{{isa = PBXBuildFile; fileRef = {pid('ref:Sources/UsageDock/Resources/Assets.xcassets')} /* Assets.xcassets */; }};\n"
    )
    build_files += "".join(build_file(path, "test:") for path in test_sources)

    unique_refs: list[str] = []
    for path in app_sources + TEST_ONLY + docs:
        if path not in unique_refs:
            unique_refs.append(path)
    file_refs = "".join(file_ref(path) for path in unique_refs)
    file_refs += file_ref("Sources/UsageDock/Resources/Assets.xcassets")
    file_refs += file_ref(PLIST)
    file_refs += file_ref(ENTITLEMENTS)
    file_refs += (
        f"\t\t{pid('ref:app')} /* UsageDock.app */ = "
        "{isa = PBXFileReference; explicitFileType = wrapper.application; includeInIndex = 0; path = UsageDock.app; sourceTree = BUILT_PRODUCTS_DIR; };\n"
        f"\t\t{pid('ref:tests')} /* UsageDockCoreTests.xctest */ = "
        "{isa = PBXFileReference; explicitFileType = wrapper.cfbundle; includeInIndex = 0; path = UsageDockCoreTests.xctest; sourceTree = BUILT_PRODUCTS_DIR; };\n"
    )

    groups = ""
    groups += group("UsageDockCore", CORE, "UsageDockCore")
    groups += group(
        "Adapters",
        [
            "Sources/UsageDock/Adapters/ProviderAdapter.swift",
            "Sources/UsageDock/Adapters/UnsupportedAdapter.swift",
            "Sources/UsageDock/Adapters/ClaudeCredentials.swift",
            "Sources/UsageDock/Adapters/ClaudeUsageClient.swift",
            "Sources/UsageDock/Adapters/ClaudeAdapter.swift",
        ],
        "Adapters",
    )
    groups += group(
        "Views",
        [
            "Sources/UsageDock/Views/ProviderMark.swift",
            "Sources/UsageDock/Views/RingGauge.swift",
            "Sources/UsageDock/Views/UsagePopover.swift",
            "Sources/UsageDock/Views/EdgeDockView.swift",
            "Sources/UsageDock/Views/SettingsView.swift",
            "Sources/UsageDock/Views/MenuBarExtraView.swift",
        ],
        "Views",
    )
    groups += group(
        "Resources",
        [
            "Sources/UsageDock/Resources/Assets.xcassets",
            PLIST,
            ENTITLEMENTS,
        ],
        "Resources",
    )
    groups += group(
        "UsageDock",
        [
            "Sources/UsageDock/Palette.swift",
            "Sources/UsageDock/AppSettings.swift",
            "Sources/UsageDock/UsageStore.swift",
            "Sources/UsageDock/UsageDockApp.swift",
            "Sources/UsageDock/DockController.swift",
            "group:Adapters",
            "group:Views",
            "group:Resources",
        ],
        "UsageDock",
    )
    # Fix UsageDock children: group ids not file refs
    groups = groups.replace(
        group(
            "UsageDock",
            [
                "Sources/UsageDock/Palette.swift",
                "Sources/UsageDock/AppSettings.swift",
                "Sources/UsageDock/UsageStore.swift",
                "Sources/UsageDock/UsageDockApp.swift",
                "Sources/UsageDock/DockController.swift",
                "group:Adapters",
                "group:Views",
                "group:Resources",
            ],
            "UsageDock",
        ),
        (
            f"\t\t{pid('group:UsageDock')} /* UsageDock */ = {{\n"
            f"\t\t\tisa = PBXGroup;\n"
            f"\t\t\tchildren = (\n"
            f"\t\t\t\t{pid('ref:Sources/UsageDock/Palette.swift')} /* Palette.swift */,\n"
            f"\t\t\t\t{pid('ref:Sources/UsageDock/AppSettings.swift')} /* AppSettings.swift */,\n"
            f"\t\t\t\t{pid('ref:Sources/UsageDock/UsageStore.swift')} /* UsageStore.swift */,\n"
            f"\t\t\t\t{pid('ref:Sources/UsageDock/UsageDockApp.swift')} /* UsageDockApp.swift */,\n"
            f"\t\t\t\t{pid('ref:Sources/UsageDock/DockController.swift')} /* DockController.swift */,\n"
            f"\t\t\t\t{pid('group:Adapters')} /* Adapters */,\n"
            f"\t\t\t\t{pid('group:Views')} /* Views */,\n"
            f"\t\t\t\t{pid('group:Resources')} /* Resources */,\n"
            f"\t\t\t);\n"
            f"\t\t\tpath = UsageDock;\n"
            f"\t\t\tsourceTree = \"<group>\";\n"
            f"\t\t}};\n"
        ),
    )
    groups += (
        f"\t\t{pid('group:Sources')} /* Sources */ = {{\n"
        f"\t\t\tisa = PBXGroup;\n"
        f"\t\t\tchildren = (\n"
        f"\t\t\t\t{pid('group:UsageDockCore')} /* UsageDockCore */,\n"
        f"\t\t\t\t{pid('group:UsageDock')} /* UsageDock */,\n"
        f"\t\t\t);\n"
        f"\t\t\tpath = Sources;\n"
        f"\t\t\tsourceTree = \"<group>\";\n"
        f"\t\t}};\n"
    )
    groups += group("UsageDockCoreTests", TEST_ONLY[:4], "UsageDockCoreTests")
    groups += (
        f"\t\t{pid('group:Tests')} /* Tests */ = {{\n"
        f"\t\t\tisa = PBXGroup;\n"
        f"\t\t\tchildren = (\n"
        f"\t\t\t\t{pid('group:UsageDockCoreTests')} /* UsageDockCoreTests */,\n"
        f"\t\t\t);\n"
        f"\t\t\tpath = Tests;\n"
        f"\t\t\tsourceTree = \"<group>\";\n"
        f"\t\t}};\n"
    )
    groups += (
        f"\t\t{pid('group:Products')} /* Products */ = {{\n"
        f"\t\t\tisa = PBXGroup;\n"
        f"\t\t\tchildren = (\n"
        f"\t\t\t\t{pid('ref:app')} /* UsageDock.app */,\n"
        f"\t\t\t\t{pid('ref:tests')} /* UsageDockCoreTests.xctest */,\n"
        f"\t\t\t);\n"
        f"\t\t\tname = Products;\n"
        f"\t\t\tsourceTree = \"<group>\";\n"
        f"\t\t}};\n"
    )
    groups += (
        f"\t\t{pid('group:root')} = {{\n"
        f"\t\t\tisa = PBXGroup;\n"
        f"\t\t\tchildren = (\n"
        f"\t\t\t\t{pid('ref:DESIGN.md')} /* DESIGN.md */,\n"
        f"\t\t\t\t{pid('ref:README.md')} /* README.md */,\n"
        f"\t\t\t\t{pid('group:Sources')} /* Sources */,\n"
        f"\t\t\t\t{pid('group:Tests')} /* Tests */,\n"
        f"\t\t\t\t{pid('group:Products')} /* Products */,\n"
        f"\t\t\t);\n"
        f"\t\t\tsourceTree = \"<group>\";\n"
        f"\t\t}};\n"
    )

    app_debug = pid("cfg:app:debug")
    app_release = pid("cfg:app:release")
    test_debug = pid("cfg:test:debug")
    test_release = pid("cfg:test:release")
    proj_debug = pid("cfg:proj:debug")
    proj_release = pid("cfg:proj:release")
    app_list = pid("list:app")
    test_list = pid("list:test")
    proj_list = pid("list:proj")
    app_sources_phase = pid("phase:app:sources")
    app_resources_phase = pid("phase:app:resources")
    app_frameworks_phase = pid("phase:app:frameworks")
    test_sources_phase = pid("phase:test:sources")
    test_frameworks_phase = pid("phase:test:frameworks")
    app_target = pid("target:app")
    test_target = pid("target:test")
    project_id = pid("project")

    project = f"""// !$*UTF8*$!
{{
	archiveVersion = 1;
	classes = {{
	}};
	objectVersion = 56;
	objects = {{

/* Begin PBXBuildFile section */
{build_files}/* End PBXBuildFile section */

/* Begin PBXFileReference section */
{file_refs}/* End PBXFileReference section */

/* Begin PBXFrameworksBuildPhase section */
		{app_frameworks_phase} /* Frameworks */ = {{
			isa = PBXFrameworksBuildPhase;
			buildActionMask = 2147483647;
			files = (
			);
			runOnlyForDeploymentPostprocessing = 0;
		}};
		{test_frameworks_phase} /* Frameworks */ = {{
			isa = PBXFrameworksBuildPhase;
			buildActionMask = 2147483647;
			files = (
			);
			runOnlyForDeploymentPostprocessing = 0;
		}};
/* End PBXFrameworksBuildPhase section */

/* Begin PBXGroup section */
{groups}/* End PBXGroup section */

/* Begin PBXNativeTarget section */
		{app_target} /* UsageDock */ = {{
			isa = PBXNativeTarget;
			buildConfigurationList = {app_list} /* Build configuration list for PBXNativeTarget "UsageDock" */;
			buildPhases = (
				{app_sources_phase} /* Sources */,
				{app_frameworks_phase} /* Frameworks */,
				{app_resources_phase} /* Resources */,
			);
			buildRules = (
			);
			dependencies = (
			);
			name = UsageDock;
			productName = UsageDock;
			productReference = {pid('ref:app')} /* UsageDock.app */;
			productType = "com.apple.product-type.application";
		}};
		{test_target} /* UsageDockCoreTests */ = {{
			isa = PBXNativeTarget;
			buildConfigurationList = {test_list} /* Build configuration list for PBXNativeTarget "UsageDockCoreTests" */;
			buildPhases = (
				{test_sources_phase} /* Sources */,
				{test_frameworks_phase} /* Frameworks */,
			);
			buildRules = (
			);
			dependencies = (
			);
			name = UsageDockCoreTests;
			productName = UsageDockCoreTests;
			productReference = {pid('ref:tests')} /* UsageDockCoreTests.xctest */;
			productType = "com.apple.product-type.bundle.unit-test";
		}};
/* End PBXNativeTarget section */

/* Begin PBXProject section */
		{project_id} /* Project object */ = {{
			isa = PBXProject;
			attributes = {{
				BuildIndependentTargetsInParallel = 1;
				LastSwiftUpdateCheck = 2700;
				LastUpgradeCheck = 2700;
				TargetAttributes = {{
					{app_target} = {{
						CreatedOnToolsVersion = 27.0;
					}};
					{test_target} = {{
						CreatedOnToolsVersion = 27.0;
					}};
				}};
			}};
			buildConfigurationList = {proj_list} /* Build configuration list for PBXProject "UsageDock" */;
			compatibilityVersion = "Xcode 14.0";
			developmentRegion = en;
			hasScannedForEncodings = 0;
			knownRegions = (
				en,
				Base,
			);
			mainGroup = {pid('group:root')};
			productRefGroup = {pid('group:Products')} /* Products */;
			projectDirPath = "";
			projectRoot = "";
			targets = (
				{app_target} /* UsageDock */,
				{test_target} /* UsageDockCoreTests */,
			);
		}};
/* End PBXProject section */

/* Begin PBXResourcesBuildPhase section */
		{app_resources_phase} /* Resources */ = {{
			isa = PBXResourcesBuildPhase;
			buildActionMask = 2147483647;
			files = (
				{pid('appres:assets')} /* Assets.xcassets in Resources */,
			);
			runOnlyForDeploymentPostprocessing = 0;
		}};
/* End PBXResourcesBuildPhase section */

{sources_phase(app_sources_phase, "Sources", app_sources, "app:")}
{sources_phase(test_sources_phase, "Sources", test_sources, "test:")}

/* Begin XCBuildConfiguration section */
{config("Debug", proj_debug, {})}
{config("Release", proj_release, {})}
{config("Debug", app_debug, {
    "ASSETCATALOG_COMPILER_APPICON_NAME": "AppIcon",
    "CODE_SIGN_ENTITLEMENTS": "Sources/UsageDock/Resources/UsageDock.entitlements",
    "CODE_SIGN_STYLE": "Automatic",
    "COMBINE_HIDPI_IMAGES": "YES",
    "CURRENT_PROJECT_VERSION": "1",
    "ENABLE_HARDENED_RUNTIME": "YES",
    "GENERATE_INFOPLIST_FILE": "NO",
    "INFOPLIST_FILE": "Sources/UsageDock/Resources/Info.plist",
    "LD_RUNPATH_SEARCH_PATHS": "\"$(inherited) @executable_path/../Frameworks\"",
    "MARKETING_VERSION": "0.1.0",
    "PRODUCT_BUNDLE_IDENTIFIER": "app.minni.UsageDock",
    "PRODUCT_NAME": "\"$(TARGET_NAME)\"",
    "SWIFT_EMIT_LOC_STRINGS": "YES",
})}
{config("Release", app_release, {
    "ASSETCATALOG_COMPILER_APPICON_NAME": "AppIcon",
    "CODE_SIGN_ENTITLEMENTS": "Sources/UsageDock/Resources/UsageDock.entitlements",
    "CODE_SIGN_STYLE": "Automatic",
    "COMBINE_HIDPI_IMAGES": "YES",
    "CURRENT_PROJECT_VERSION": "1",
    "ENABLE_HARDENED_RUNTIME": "YES",
    "GENERATE_INFOPLIST_FILE": "NO",
    "INFOPLIST_FILE": "Sources/UsageDock/Resources/Info.plist",
    "LD_RUNPATH_SEARCH_PATHS": "\"$(inherited) @executable_path/../Frameworks\"",
    "MARKETING_VERSION": "0.1.0",
    "PRODUCT_BUNDLE_IDENTIFIER": "app.minni.UsageDock",
    "PRODUCT_NAME": "\"$(TARGET_NAME)\"",
    "SWIFT_EMIT_LOC_STRINGS": "YES",
})}
{config("Debug", test_debug, {
    "BUNDLE_LOADER": "\"$(TEST_HOST)\"",
    "CODE_SIGN_STYLE": "Automatic",
    "GENERATE_INFOPLIST_FILE": "YES",
    "PRODUCT_BUNDLE_IDENTIFIER": "app.minni.UsageDockTests",
    "PRODUCT_NAME": "\"$(TARGET_NAME)\"",
    "TEST_HOST": "\"$(BUILT_PRODUCTS_DIR)/UsageDock.app/Contents/MacOS/UsageDock\"",
})}
{config("Release", test_release, {
    "BUNDLE_LOADER": "\"$(TEST_HOST)\"",
    "CODE_SIGN_STYLE": "Automatic",
    "GENERATE_INFOPLIST_FILE": "YES",
    "PRODUCT_BUNDLE_IDENTIFIER": "app.minni.UsageDockTests",
    "PRODUCT_NAME": "\"$(TARGET_NAME)\"",
    "TEST_HOST": "\"$(BUILT_PRODUCTS_DIR)/UsageDock.app/Contents/MacOS/UsageDock\"",
})}
/* End XCBuildConfiguration section */

/* Begin XCConfigurationList section */
		{proj_list} /* Build configuration list for PBXProject "UsageDock" */ = {{
			isa = XCConfigurationList;
			buildConfigurations = (
				{proj_debug} /* Debug */,
				{proj_release} /* Release */,
			);
			defaultConfigurationIsVisible = 0;
			defaultConfigurationName = Release;
		}};
		{app_list} /* Build configuration list for PBXNativeTarget "UsageDock" */ = {{
			isa = XCConfigurationList;
			buildConfigurations = (
				{app_debug} /* Debug */,
				{app_release} /* Release */,
			);
			defaultConfigurationIsVisible = 0;
			defaultConfigurationName = Release;
		}};
		{test_list} /* Build configuration list for PBXNativeTarget "UsageDockCoreTests" */ = {{
			isa = XCConfigurationList;
			buildConfigurations = (
				{test_debug} /* Debug */,
				{test_release} /* Release */,
			);
			defaultConfigurationIsVisible = 0;
			defaultConfigurationName = Release;
		}};
/* End XCConfigurationList section */

	}};
	rootObject = {project_id} /* Project object */;
}}
"""
    dest = ROOT / "UsageDock.xcodeproj" / "project.pbxproj"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(project)

    scheme = f"""<?xml version="1.0" encoding="UTF-8"?>
<Scheme
   LastUpgradeVersion = "2700"
   version = "1.7">
   <BuildAction
      parallelizeBuildables = "YES"
      buildImplicitDependencies = "YES">
      <BuildActionEntries>
         <BuildActionEntry
            buildForTesting = "YES"
            buildForRunning = "YES"
            buildForProfiling = "YES"
            buildForArchiving = "YES"
            buildForAnalyzing = "YES">
            <BuildableReference
               BuildableIdentifier = "primary"
               BlueprintIdentifier = "{app_target}"
               BuildableName = "UsageDock.app"
               BlueprintName = "UsageDock"
               ReferencedContainer = "container:UsageDock.xcodeproj">
            </BuildableReference>
         </BuildActionEntry>
      </BuildActionEntries>
   </BuildAction>
   <TestAction
      buildConfiguration = "Debug"
      selectedDebuggerIdentifier = "Xcode.DebuggerFoundation.Debugger.LLDB"
      selectedLauncherIdentifier = "Xcode.DebuggerFoundation.Launcher.LLDB"
      shouldUseLaunchSchemeArgsEnv = "YES">
      <Testables>
         <TestableReference
            skipped = "NO">
            <BuildableReference
               BuildableIdentifier = "primary"
               BlueprintIdentifier = "{test_target}"
               BuildableName = "UsageDockCoreTests.xctest"
               BlueprintName = "UsageDockCoreTests"
               ReferencedContainer = "container:UsageDock.xcodeproj">
            </BuildableReference>
         </TestableReference>
      </Testables>
   </TestAction>
   <LaunchAction
      buildConfiguration = "Debug"
      selectedDebuggerIdentifier = "Xcode.DebuggerFoundation.Debugger.LLDB"
      selectedLauncherIdentifier = "Xcode.DebuggerFoundation.Launcher.LLDB"
      launchStyle = "0"
      useCustomWorkingDirectory = "NO"
      ignoresPersistentStateOnLaunch = "NO"
      debugDocumentVersioning = "YES"
      debugServiceExtension = "internal"
      allowLocationSimulation = "YES">
      <BuildableProductRunnable
         runnableDebuggingMode = "0">
         <BuildableReference
            BuildableIdentifier = "primary"
            BlueprintIdentifier = "{app_target}"
            BuildableName = "UsageDock.app"
            BlueprintName = "UsageDock"
            ReferencedContainer = "container:UsageDock.xcodeproj">
         </BuildableReference>
      </BuildableProductRunnable>
   </LaunchAction>
   <ProfileAction
      buildConfiguration = "Release"
      shouldUseLaunchSchemeArgsEnv = "YES"
      savedToolIdentifier = ""
      useCustomWorkingDirectory = "NO"
      debugDocumentVersioning = "YES">
      <BuildableProductRunnable
         runnableDebuggingMode = "0">
         <BuildableReference
            BuildableIdentifier = "primary"
            BlueprintIdentifier = "{app_target}"
            BuildableName = "UsageDock.app"
            BlueprintName = "UsageDock"
            ReferencedContainer = "container:UsageDock.xcodeproj">
         </BuildableReference>
      </BuildableProductRunnable>
   </ProfileAction>
   <AnalyzeAction
      buildConfiguration = "Debug">
   </AnalyzeAction>
   <ArchiveAction
      buildConfiguration = "Release"
      revealArchiveInOrganizer = "YES">
   </ArchiveAction>
</Scheme>
"""
    scheme_dir = ROOT / "UsageDock.xcodeproj" / "xcshareddata" / "xcschemes"
    scheme_dir.mkdir(parents=True, exist_ok=True)
    (scheme_dir / "UsageDock.xcscheme").write_text(scheme)
    print(f"wrote {dest}")
    print(f"wrote {scheme_dir / 'UsageDock.xcscheme'}")


if __name__ == "__main__":
    main()
