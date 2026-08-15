#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "VistaInteractable.h"
#include "VistaSemanticActor.generated.h"

UCLASS(Abstract, Blueprintable)
class VISTAPLAYABLEHOME_API AVistaSemanticActor : public AActor, public IVistaInteractable
{
    GENERATED_BODY()

public:
    AVistaSemanticActor();

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Identity")
    FString SemanticId;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Identity")
    FName WorldRevision = NAME_None;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Interaction")
    TArray<EVistaAffordance> AllowedAffordances;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|State")
    TMap<FName, FString> InitialStateValues;

    virtual FString VistaGetSemanticId_Implementation() const override;
    virtual TArray<EVistaAffordance> VistaGetAffordances_Implementation() const override;
    virtual FVistaEntityRuntimeState VistaGetRuntimeState_Implementation() const override;
    virtual FVistaInteractionResult VistaApplyRuntimeState_Implementation(
        const FVistaEntityRuntimeState& State) override;
    virtual FVistaInteractionResult VistaInteract_Implementation(
        const FVistaInteractionRequest& Request) override;

protected:
    virtual void BeginPlay() override;

    bool Supports(EVistaAffordance Affordance) const;
    FVistaInteractionResult ValidateRequest(const FVistaInteractionRequest& Request) const;

    UPROPERTY(VisibleInstanceOnly, BlueprintReadOnly, Category = "VISTA|State")
    TMap<FName, FString> RuntimeStateValues;
};
