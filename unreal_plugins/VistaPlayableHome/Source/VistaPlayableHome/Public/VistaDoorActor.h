#pragma once

#include "CoreMinimal.h"
#include "VistaSemanticActor.h"
#include "VistaDoorActor.generated.h"

class UNavLinkCustomComponent;
class UPathFollowingComponent;
class USceneComponent;
class UStaticMeshComponent;

UCLASS(Blueprintable)
class VISTAPLAYABLEHOME_API AVistaDoorActor final : public AVistaSemanticActor
{
    GENERATED_BODY()

public:
    AVistaDoorActor();

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Door")
    TObjectPtr<USceneComponent> SceneRoot;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Door")
    TObjectPtr<USceneComponent> Hinge;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Door")
    TObjectPtr<UStaticMeshComponent> DoorMesh;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Door")
    TObjectPtr<UNavLinkCustomComponent> DoorwayLink;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Door",
              meta = (ClampMin = "-170.0", ClampMax = "170.0"))
    float OpenAngleDegrees = 90.0f;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Door",
              meta = (ClampMin = "10.0", ClampMax = "720.0"))
    float AngularSpeedDegrees = 180.0f;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Door")
    bool bInitiallyOpen = false;

    UFUNCTION(BlueprintPure, Category = "VISTA|Door")
    bool IsOpen() const { return bOpen; }

    virtual FVistaEntityRuntimeState VistaGetRuntimeState_Implementation() const override;
    virtual FVistaInteractionResult VistaApplyRuntimeState_Implementation(
        const FVistaEntityRuntimeState& State) override;
    virtual FVistaInteractionResult VistaInteract_Implementation(
        const FVistaInteractionRequest& Request) override;
    virtual void GetLifetimeReplicatedProps(
        TArray<FLifetimeProperty>& OutLifetimeProps) const override;
    virtual void Tick(float DeltaSeconds) override;

protected:
    virtual void BeginPlay() override;

private:
    UPROPERTY(ReplicatedUsing = OnRep_OpenState)
    bool bOpen = false;

    FRotator ClosedRotation = FRotator::ZeroRotator;
    FRotator TargetRotation = FRotator::ZeroRotator;

    UFUNCTION()
    void OnRep_OpenState();

    void HandleDoorwayLinkReached(UNavLinkCustomComponent* LinkComponent,
                                  UObject* PathingAgent,
                                  const FVector& Destination);
    void ConfigureJambPivot();
    bool IsClosingObstructed() const;
    void ApplyDoorState(bool bInstant);
};
