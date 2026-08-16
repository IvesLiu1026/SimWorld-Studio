#pragma once

#include "CoreMinimal.h"
#include "GameFramework/SpringArmComponent.h"
#include "VistaIndoorSpringArmComponent.generated.h"

/**
 * Spring arm used by the opt-in realistic-interior camera profile.
 *
 * The stock spring arm already prevents the camera from crossing blocking
 * geometry.  This subclass preserves that immediate inward correction, then
 * damps only the outward recovery.  The recovered point stays on the current
 * collision-tested arm segment, so a doorway transition cannot leave the
 * camera lingering behind a wall.
 *
 * Recovery is disabled by default.  With no r2 profile selected this class
 * delegates exactly to USpringArmComponent::BlendLocations.
 */
UCLASS(ClassGroup = (VISTA), meta = (BlueprintSpawnableComponent))
class VISTAPLAYABLEHOME_API UVistaIndoorSpringArmComponent final
    : public USpringArmComponent
{
    GENERATED_BODY()

public:
    void ConfigureIndoorCollisionRecovery(
        bool bEnabled,
        float InRecoverySpeed,
        float InSnapThresholdCm);

    void ResetIndoorCollisionRecovery();

    UFUNCTION(BlueprintPure, Category = "VISTA|Camera")
    bool IsIndoorCollisionRecoveryEnabled() const
    {
        return bIndoorCollisionRecoveryEnabled;
    }

protected:
    virtual FVector BlendLocations(
        const FVector& DesiredArmLocation,
        const FVector& TraceHitLocation,
        bool bHitSomething,
        float DeltaTime) override;

private:
    bool bIndoorCollisionRecoveryEnabled = false;
    bool bHasCollisionCompression = false;
    float RecoveryArmFraction = 1.0f;
    float CollisionRecoverySpeed = 8.0f;
    float RecoverySnapThresholdCm = 1.0f;
};
