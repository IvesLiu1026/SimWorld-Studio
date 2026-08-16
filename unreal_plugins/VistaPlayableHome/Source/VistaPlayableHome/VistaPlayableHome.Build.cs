using UnrealBuildTool;

public class VistaPlayableHome : ModuleRules
{
    public VistaPlayableHome(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        CppStandard = CppStandardVersion.Cpp20;

        PublicDependencyModuleNames.AddRange(new[]
        {
            "Core",
            "CoreUObject",
            "Engine",
            "InputCore",
            "EnhancedInput",
            "AIModule",
            "NavigationSystem",
            "GameplayTasks",
            "PhysicsCore",
            "RHI",
            "Sockets",
            "Networking",
            "Json"
        });
    }
}
