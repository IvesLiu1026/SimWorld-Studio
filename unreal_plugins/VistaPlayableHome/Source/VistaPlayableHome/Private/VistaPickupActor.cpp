#include "VistaPickupActor.h"

#include "Components/StaticMeshComponent.h"
#include "Engine/CollisionProfile.h"
#include "EngineUtils.h"
#include "Net/UnrealNetwork.h"
#include "VistaHomeNpcCharacter.h"
#include "VistaItemCarrier.h"
#include "VistaPlayableHomeCharacter.h"

namespace
{
FString CarrierSemanticId(const AActor* Carrier)
{
    if (const AVistaPlayableHomeCharacter* Player =
            Cast<AVistaPlayableHomeCharacter>(Carrier))
    {
        return Player->SemanticId;
    }
    if (const AVistaHomeNpcCharacter* Npc = Cast<AVistaHomeNpcCharacter>(Carrier))
    {
        return Npc->SemanticId;
    }
    if (IsValid(Carrier) &&
        Carrier->GetClass()->ImplementsInterface(UVistaInteractable::StaticClass()))
    {
        return IVistaInteractable::Execute_VistaGetSemanticId(
            const_cast<AActor*>(Carrier));
    }
    return FString();
}

AActor* ResolveCarrier(UWorld* World, const FString& SemanticId)
{
    if (!IsValid(World) || SemanticId.IsEmpty())
    {
        return nullptr;
    }
    const FName RawTag(*SemanticId);
    const FName StableTag(*(FString(TEXT("VistaSemanticId=")) + SemanticId));
    for (TActorIterator<AActor> It(World); It; ++It)
    {
        if (It->ActorHasTag(RawTag) || It->ActorHasTag(StableTag))
        {
            return *It;
        }
    }
    return nullptr;
}
}

AVistaPickupActor::AVistaPickupActor()
{
    Mesh = CreateDefaultSubobject<UStaticMeshComponent>(TEXT("PickupMesh"));
    SetRootComponent(Mesh);
    Mesh->SetCollisionProfileName(UCollisionProfile::PhysicsActor_ProfileName);
    Mesh->SetSimulatePhysics(true);
    Mesh->SetGenerateOverlapEvents(true);
    AllowedAffordances = {
        EVistaAffordance::Inspect,
        EVistaAffordance::PickUp,
        EVistaAffordance::Drop,
        EVistaAffordance::Place};
}

void AVistaPickupActor::GetLifetimeReplicatedProps(
    TArray<FLifetimeProperty>& OutLifetimeProps) const
{
    Super::GetLifetimeReplicatedProps(OutLifetimeProps);
    DOREPLIFETIME(AVistaPickupActor, HeldBy);
}

FVistaEntityRuntimeState AVistaPickupActor::VistaGetRuntimeState_Implementation() const
{
    FVistaEntityRuntimeState State = Super::VistaGetRuntimeState_Implementation();
    State.bPortable = bPortable;
    State.Values.Add(TEXT("held"), IsValid(HeldBy) ? TEXT("true") : TEXT("false"));
    State.Values.Add(TEXT("held_by"), CarrierSemanticId(HeldBy));
    return State;
}

FVistaInteractionResult AVistaPickupActor::VistaApplyRuntimeState_Implementation(
    const FVistaEntityRuntimeState& State)
{
    if (!HasAuthority())
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::Rejected, TEXT("AUTHORITY_REQUIRED"), SemanticId);
    }
    if (IsValid(HeldBy))
    {
        ReleaseFromCarrier();
    }
    bPortable = State.bPortable;
    const FString* HeldValue = State.Values.Find(TEXT("held"));
    const bool bRestoreHeld = HeldValue &&
        HeldValue->Equals(TEXT("true"), ESearchCase::IgnoreCase);
    const FString* DesiredCarrierId = State.Values.Find(TEXT("held_by"));
    const FVistaInteractionResult BaseResult = Super::VistaApplyRuntimeState_Implementation(State);
    if (!BaseResult.IsSuccess())
    {
        return BaseResult;
    }
    Mesh->SetSimulatePhysics(bPortable);
    if (bRestoreHeld)
    {
        AActor* Carrier = DesiredCarrierId
            ? ResolveCarrier(GetWorld(), *DesiredCarrierId)
            : nullptr;
        if (!IsValid(Carrier))
        {
            return FVistaInteractionResult::Failure(
                EVistaInteractionStatus::NotFound,
                TEXT("BASELINE_CARRIER_NOT_FOUND"), SemanticId);
        }
        const FVistaInteractionResult AttachResult = TryAttachTo(Carrier);
        if (!AttachResult.IsSuccess())
        {
            return AttachResult;
        }
    }
    return FVistaInteractionResult::Success(
        SemanticId, VistaGetRuntimeState_Implementation(), TEXT("PICKUP_STATE_APPLIED"));
}

FVistaInteractionResult AVistaPickupActor::VistaInteract_Implementation(
    const FVistaInteractionRequest& Request)
{
    const FVistaInteractionResult Validation = ValidateRequest(Request);
    if (!Validation.IsSuccess())
    {
        return Validation;
    }
    if (!HasAuthority())
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::Rejected, TEXT("AUTHORITY_REQUIRED"), SemanticId);
    }

    switch (Request.Affordance)
    {
    case EVistaAffordance::PickUp:
        return TryAttachTo(Request.Requester);
    case EVistaAffordance::Drop:
        if (HeldBy != Request.Requester)
        {
            return FVistaInteractionResult::Failure(
                EVistaInteractionStatus::InvalidRequester, TEXT("NOT_ITEM_CARRIER"), SemanticId);
        }
        return ReleaseFromCarrier();
    case EVistaAffordance::Place:
        if (HeldBy != Request.Requester || !IsValid(Request.PlacementAnchor))
        {
            return FVistaInteractionResult::Failure(
                EVistaInteractionStatus::InvalidState, TEXT("PLACEMENT_ANCHOR_REQUIRED"), SemanticId);
        }
        return ReleaseFromCarrier(FVector::ZeroVector, Request.PlacementAnchor);
    default:
        return Super::VistaInteract_Implementation(Request);
    }
}

FVistaInteractionResult AVistaPickupActor::TryAttachTo(AActor* Carrier)
{
    if (!bPortable)
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::InvalidState, TEXT("ITEM_NOT_PORTABLE"), SemanticId);
    }
    if (IsValid(HeldBy))
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::Busy, TEXT("ITEM_ALREADY_HELD"), SemanticId);
    }
    if (!IsValid(Carrier) || !Carrier->GetClass()->ImplementsInterface(UVistaItemCarrier::StaticClass()))
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::InvalidRequester, TEXT("CARRIER_REQUIRED"), SemanticId);
    }
    USceneComponent* Anchor = IVistaItemCarrier::Execute_VistaGetCarryAnchor(Carrier);
    if (!IsValid(Anchor) || !IVistaItemCarrier::Execute_VistaTryClaimItem(Carrier, this))
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::Busy, TEXT("CARRIER_SLOT_UNAVAILABLE"), SemanticId);
    }

    HeldBy = Carrier;
    ApplyAttachmentState();
    ForceNetUpdate();
    return FVistaInteractionResult::Success(
        SemanticId, VistaGetRuntimeState_Implementation(), TEXT("ITEM_PICKED_UP"));
}

FVistaInteractionResult AVistaPickupActor::ReleaseFromCarrier(
    const FVector& LinearVelocity,
    USceneComponent* PlacementAnchor)
{
    if (!HasAuthority())
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::Rejected, TEXT("AUTHORITY_REQUIRED"), SemanticId);
    }
    if (!IsValid(HeldBy))
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::InvalidState, TEXT("ITEM_NOT_HELD"), SemanticId);
    }

    AActor* PreviousCarrier = HeldBy;
    const FTransform ReleaseTransform = IsValid(PlacementAnchor)
        ? PlacementAnchor->GetComponentTransform()
        : GetActorTransform();
    DetachFromActor(FDetachmentTransformRules::KeepWorldTransform);
    HeldBy = nullptr;
    Mesh->SetCollisionProfileName(UCollisionProfile::PhysicsActor_ProfileName);
    Mesh->SetSimulatePhysics(!IsValid(PlacementAnchor));
    SetActorTransform(ReleaseTransform, false, nullptr, ETeleportType::TeleportPhysics);
    if (!IsValid(PlacementAnchor))
    {
        Mesh->SetPhysicsLinearVelocity(LinearVelocity);
    }
    IVistaItemCarrier::Execute_VistaReleaseItem(PreviousCarrier, this);
    ForceNetUpdate();
    return FVistaInteractionResult::Success(
        SemanticId, VistaGetRuntimeState_Implementation(),
        IsValid(PlacementAnchor) ? TEXT("ITEM_PLACED") : TEXT("ITEM_DROPPED"));
}

void AVistaPickupActor::OnRep_HeldBy()
{
    ApplyAttachmentState();
}

void AVistaPickupActor::ApplyAttachmentState()
{
    if (IsValid(HeldBy) && HeldBy->GetClass()->ImplementsInterface(UVistaItemCarrier::StaticClass()))
    {
        if (USceneComponent* Anchor = IVistaItemCarrier::Execute_VistaGetCarryAnchor(HeldBy))
        {
            Mesh->SetSimulatePhysics(false);
            Mesh->SetCollisionEnabled(ECollisionEnabled::NoCollision);
            AttachToComponent(Anchor, FAttachmentTransformRules::SnapToTargetNotIncludingScale);
            return;
        }
    }
    DetachFromActor(FDetachmentTransformRules::KeepWorldTransform);
    Mesh->SetCollisionProfileName(UCollisionProfile::PhysicsActor_ProfileName);
    Mesh->SetSimulatePhysics(bPortable);
}
