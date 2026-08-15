#pragma once

#include "CoreMinimal.h"
#include "VistaSemanticActor.h"
#include "VistaStatefulApplianceActor.generated.h"

class UStaticMeshComponent;

UCLASS(Blueprintable)
class VISTAPLAYABLEHOME_API AVistaStatefulApplianceActor final : public AVistaSemanticActor
{
    GENERATED_BODY()

public:
    AVistaStatefulApplianceActor();

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Appliance")
    TObjectPtr<UStaticMeshComponent> Mesh;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Appliance")
    FName ApplianceKind = TEXT("generic");

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Appliance")
    bool bInitiallyOn = false;

    UFUNCTION(BlueprintPure, Category = "VISTA|Appliance")
    bool IsOn() const { return bOn; }

    virtual FVistaEntityRuntimeState VistaGetRuntimeState_Implementation() const override;
    virtual FVistaInteractionResult VistaApplyRuntimeState_Implementation(
        const FVistaEntityRuntimeState& State) override;
    virtual FVistaInteractionResult VistaInteract_Implementation(
        const FVistaInteractionRequest& Request) override;
    virtual void GetLifetimeReplicatedProps(
        TArray<FLifetimeProperty>& OutLifetimeProps) const override;

protected:
    virtual void BeginPlay() override;

    UFUNCTION(BlueprintImplementableEvent, Category = "VISTA|Appliance")
    void OnApplianceStateChanged(bool bNewOn);

private:
    UPROPERTY(ReplicatedUsing = OnRep_OnState)
    bool bOn = false;

    UFUNCTION()
    void OnRep_OnState();
};
