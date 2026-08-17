#pragma once

#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "VistaPlayableHomeNaniteLibrary.generated.h"

/**
 * Native editor-only bridge for the deterministic VISTA imported-material
 * parent chain and Nanite policy. UObject mutation deliberately does not cross
 * the Python boundary.
 */
UCLASS()
class VISTAPLAYABLEHOMEEDITOR_API UVistaPlayableHomeNaniteLibrary final
    : public UBlueprintFunctionLibrary
{
    GENERATED_BODY()

public:
    /**
     * Finalize Nanite policy for revision-private imported static meshes.
     *
     * MeshObjectPaths must contain full object paths, not package paths. The
     * return value is deterministic condensed JSON with schema_version
     * simworld.vista.playable-home-native-nanite/v1 and status success or
     * failed.
     */
    UFUNCTION(BlueprintCallable, Category = "VISTA|Editor|Nanite")
    static FString FinalizeNanitePolicies(
        const FString& RevisionNamespace,
        const TArray<FString>& MeshObjectPaths);
};
