#pragma once

#include "CoreMinimal.h"
#include "UObject/Interface.h"
#include "VistaPlayableHomeTypes.h"
#include "VistaInteractable.generated.h"

UINTERFACE(BlueprintType)
class VISTAPLAYABLEHOME_API UVistaInteractable : public UInterface
{
    GENERATED_BODY()
};

class VISTAPLAYABLEHOME_API IVistaInteractable
{
    GENERATED_BODY()

public:
    UFUNCTION(BlueprintNativeEvent, BlueprintCallable, Category = "VISTA|Interaction")
    FString VistaGetSemanticId() const;

    UFUNCTION(BlueprintNativeEvent, BlueprintCallable, Category = "VISTA|Interaction")
    TArray<EVistaAffordance> VistaGetAffordances() const;

    UFUNCTION(BlueprintNativeEvent, BlueprintCallable, Category = "VISTA|Interaction")
    FVistaEntityRuntimeState VistaGetRuntimeState() const;

    UFUNCTION(BlueprintNativeEvent, BlueprintCallable, Category = "VISTA|Interaction")
    FVistaInteractionResult VistaApplyRuntimeState(const FVistaEntityRuntimeState& State);

    UFUNCTION(BlueprintNativeEvent, BlueprintCallable, Category = "VISTA|Interaction")
    FVistaInteractionResult VistaInteract(const FVistaInteractionRequest& Request);
};
