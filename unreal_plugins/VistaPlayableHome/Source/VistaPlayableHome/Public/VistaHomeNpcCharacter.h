#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Character.h"
#include "VistaInteractable.h"
#include "VistaItemCarrier.h"
#include "VistaHomeNpcCharacter.generated.h"

class AVistaPickupActor;
class USceneComponent;

UCLASS(Blueprintable)
class VISTAPLAYABLEHOME_API AVistaHomeNpcCharacter final
    : public ACharacter,
      public IVistaInteractable,
      public IVistaItemCarrier
{
    GENERATED_BODY()

public:
    AVistaHomeNpcCharacter();

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Identity")
    FString SemanticId;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Identity")
    FName WorldRevision = NAME_None;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA|Location")
    FString CurrentRoomId;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Interaction")
    TArray<EVistaAffordance> AllowedAffordances;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Patrol")
    TArray<FString> PatrolTargetSemanticIds;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Patrol",
              meta = (ClampMin = "0.0", ClampMax = "300.0"))
    float PatrolActionTimeoutSeconds = 20.0f;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Patrol")
    bool bAutoStartPatrol = true;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Carry")
    TObjectPtr<USceneComponent> CarryAnchor;

    virtual FString VistaGetSemanticId_Implementation() const override;
    virtual TArray<EVistaAffordance> VistaGetAffordances_Implementation() const override;
    virtual FVistaEntityRuntimeState VistaGetRuntimeState_Implementation() const override;
    virtual FVistaInteractionResult VistaApplyRuntimeState_Implementation(
        const FVistaEntityRuntimeState& State) override;
    virtual FVistaInteractionResult VistaInteract_Implementation(
        const FVistaInteractionRequest& Request) override;

    virtual USceneComponent* VistaGetCarryAnchor_Implementation() const override;
    virtual AActor* VistaGetHeldItem_Implementation() const override;
    virtual bool VistaTryClaimItem_Implementation(AActor* Item) override;
    virtual void VistaReleaseItem_Implementation(AActor* Item) override;

    virtual void GetLifetimeReplicatedProps(
        TArray<FLifetimeProperty>& OutLifetimeProps) const override;

protected:
    virtual void BeginPlay() override;

private:
    UPROPERTY(Replicated)
    TObjectPtr<AVistaPickupActor> HeldItem = nullptr;
};
