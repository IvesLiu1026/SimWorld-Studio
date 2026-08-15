#pragma once

#include "CoreMinimal.h"
#include "VistaSemanticActor.h"
#include "VistaContainerActor.generated.h"

class UStaticMeshComponent;

UCLASS(Blueprintable)
class VISTAPLAYABLEHOME_API AVistaContainerActor final : public AVistaSemanticActor
{
    GENERATED_BODY()

public:
    AVistaContainerActor();

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Container")
    TObjectPtr<UStaticMeshComponent> Mesh;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Container")
    bool bInitiallyOpen = false;

    virtual FVistaEntityRuntimeState VistaGetRuntimeState_Implementation() const override;
    virtual FVistaInteractionResult VistaApplyRuntimeState_Implementation(
        const FVistaEntityRuntimeState& State) override;
    virtual FVistaInteractionResult VistaInteract_Implementation(
        const FVistaInteractionRequest& Request) override;
    virtual void GetLifetimeReplicatedProps(
        TArray<FLifetimeProperty>& OutLifetimeProps) const override;

protected:
    virtual void BeginPlay() override;

    UFUNCTION(BlueprintImplementableEvent, Category = "VISTA|Container")
    void OnContainerStateChanged(bool bNewOpen);

private:
    UPROPERTY(ReplicatedUsing = OnRep_OpenState)
    bool bOpen = false;

    UFUNCTION()
    void OnRep_OpenState();
};
