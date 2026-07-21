using System;
using System.Text.RegularExpressions;
using UnrealBuildTool;

public class VistaAnimationContentApi : ModuleRules
{
    public VistaAnimationContentApi(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        CppStandard = CppStandardVersion.Cpp20;

        PublicDependencyModuleNames.AddRange(new[]
        {
            "Core",
            "CoreUObject",
            "Engine"
        });

        string BuildId = Environment.GetEnvironmentVariable("VISTA_ANIMATION_PLUGIN_BUILD_ID")
            ?? "vista-animation-local-unverified";
        if (!Regex.IsMatch(BuildId, "^[A-Za-z0-9][A-Za-z0-9._:@-]{0,159}$"))
        {
            throw new BuildException("VISTA_ANIMATION_PLUGIN_BUILD_ID is not a safe opaque identifier");
        }

        PublicDefinitions.Add("VISTA_ANIMATION_PLUGIN_VERSION=TEXT(\"1.1.0\")");
        PublicDefinitions.Add($"VISTA_ANIMATION_PLUGIN_BUILD_ID=TEXT(\"{BuildId}\")");
    }
}
