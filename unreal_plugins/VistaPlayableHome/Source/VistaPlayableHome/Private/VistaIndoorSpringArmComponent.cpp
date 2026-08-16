#include "VistaIndoorSpringArmComponent.h"

void UVistaIndoorSpringArmComponent::ConfigureIndoorCollisionRecovery(
    bool bEnabled,
    float InRecoverySpeed,
    float InSnapThresholdCm)
{
    bIndoorCollisionRecoveryEnabled = bEnabled;
    CollisionRecoverySpeed = InRecoverySpeed;
    RecoverySnapThresholdCm = InSnapThresholdCm;
    ResetIndoorCollisionRecovery();
}

void UVistaIndoorSpringArmComponent::ResetIndoorCollisionRecovery()
{
    bHasCollisionCompression = false;
    RecoveryArmFraction = 1.0f;
}

FVector UVistaIndoorSpringArmComponent::BlendLocations(
    const FVector& DesiredArmLocation,
    const FVector& TraceHitLocation,
    bool bHitSomething,
    float DeltaTime)
{
    const FVector StockLocation = Super::BlendLocations(
        DesiredArmLocation,
        TraceHitLocation,
        bHitSomething,
        DeltaTime);
    if (!bIndoorCollisionRecoveryEnabled)
    {
        return StockLocation;
    }

    // TargetOffset is applied to the spring-arm origin before its collision
    // trace.  Reconstruct the same origin so the smoothed point remains on the
    // trace-tested segment rather than taking a world-space shortcut.
    const FVector ArmOrigin = GetComponentLocation() + TargetOffset;
    const float DesiredDistance = FVector::Distance(ArmOrigin, DesiredArmLocation);
    if (DesiredDistance <= UE_KINDA_SMALL_NUMBER)
    {
        ResetIndoorCollisionRecovery();
        return StockLocation;
    }

    const float TargetFraction = bHitSomething
        ? FMath::Clamp(
              FVector::Distance(ArmOrigin, StockLocation) / DesiredDistance,
              0.0f,
              1.0f)
        : 1.0f;

    if (bHitSomething &&
        (!bHasCollisionCompression || TargetFraction <= RecoveryArmFraction))
    {
        // Never ease inward through a newly encountered wall.  The stock hit
        // location is accepted immediately and is therefore collision-safe.
        RecoveryArmFraction = TargetFraction;
        bHasCollisionCompression = true;
    }
    else if (bHasCollisionCompression)
    {
        // An outward move is safe only up to TargetFraction.  FInterpTo cannot
        // overshoot it, keeping recovery on the current clear trace segment.
        RecoveryArmFraction = FMath::FInterpTo(
            RecoveryArmFraction,
            TargetFraction,
            FMath::Max(DeltaTime, 0.0f),
            CollisionRecoverySpeed);
    }
    else
    {
        return StockLocation;
    }

    if (!bHitSomething &&
        (1.0f - RecoveryArmFraction) * DesiredDistance <= RecoverySnapThresholdCm)
    {
        ResetIndoorCollisionRecovery();
        return StockLocation;
    }

    return FMath::Lerp(ArmOrigin, DesiredArmLocation, RecoveryArmFraction);
}
