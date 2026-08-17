using UnrealBuildTool;

public class VistaPlayableHomeEditor : ModuleRules
{
    public VistaPlayableHomeEditor(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        CppStandard = CppStandardVersion.Cpp20;

        PublicDependencyModuleNames.AddRange(new[]
        {
            "Core",
            "CoreUObject",
            "Engine"
        });

        PrivateDependencyModuleNames.AddRange(new[]
        {
            "AssetTools",
            "Json",
            "MaterialEditor"
        });
    }
}
