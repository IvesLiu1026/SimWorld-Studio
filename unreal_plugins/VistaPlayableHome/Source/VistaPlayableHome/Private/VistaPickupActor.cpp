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
constexpr const TCHAR* StableSemanticTagPrefix = TEXT("VistaSemanticId=");
constexpr const TCHAR* PlacementOwnerTagPrefix = TEXT("VistaOwner=");
constexpr const TCHAR* PlacementAnchorDelimiter = TEXT("/anchor.");

bool IsLowerAsciiAlpha(const TCHAR Character)
{
    return Character >= TEXT('a') && Character <= TEXT('z');
}

bool IsAsciiDigit(const TCHAR Character)
{
    return Character >= TEXT('0') && Character <= TEXT('9');
}

bool IsStablePlacementAnchorSemanticId(const FString& Value)
{
    if (Value.IsEmpty() || Value.Len() > 240 || !IsLowerAsciiAlpha(Value[0]) ||
        Value.Contains(TEXT("#")))
    {
        return false;
    }
    for (const TCHAR Character : Value)
    {
        if (!(IsLowerAsciiAlpha(Character) || IsAsciiDigit(Character) ||
              Character == TEXT('.') || Character == TEXT('_') ||
              Character == TEXT('/') || Character == TEXT('-')))
        {
            return false;
        }
    }

    const int32 DelimiterIndex = Value.Find(
        PlacementAnchorDelimiter, ESearchCase::CaseSensitive, ESearchDir::FromStart);
    if (DelimiterIndex <= 0 ||
        DelimiterIndex + FCString::Strlen(PlacementAnchorDelimiter) >= Value.Len() ||
        Value.Find(PlacementAnchorDelimiter, ESearchCase::CaseSensitive,
                   ESearchDir::FromStart, DelimiterIndex + 1) != INDEX_NONE)
    {
        return false;
    }
    const FString AnchorId = Value.Mid(
        DelimiterIndex + FCString::Strlen(PlacementAnchorDelimiter));
    const FString OwnerSemanticId = Value.Left(DelimiterIndex);
    if (!OwnerSemanticId.Contains(TEXT("/entity."), ESearchCase::CaseSensitive) ||
        AnchorId.IsEmpty() || !IsLowerAsciiAlpha(AnchorId[0]))
    {
        return false;
    }
    for (const TCHAR Character : AnchorId)
    {
        if (!(IsLowerAsciiAlpha(Character) || IsAsciiDigit(Character) ||
              Character == TEXT('_')))
        {
            return false;
        }
    }
    return true;
}

bool CanonicalizePlacementAnchorSemanticId(const FString& Value, FString& OutSemanticId)
{
    FString Candidate = Value;
    int32 CompactDelimiterIndex = INDEX_NONE;
    if (Candidate.FindChar(TEXT('#'), CompactDelimiterIndex))
    {
        if (CompactDelimiterIndex <= 0 || CompactDelimiterIndex + 1 >= Candidate.Len() ||
            Candidate.Mid(CompactDelimiterIndex + 1).Contains(TEXT("#")))
        {
            return false;
        }
        Candidate = Candidate.Left(CompactDelimiterIndex) + PlacementAnchorDelimiter +
            Candidate.Mid(CompactDelimiterIndex + 1);
    }
    if (!IsStablePlacementAnchorSemanticId(Candidate))
    {
        return false;
    }
    OutSemanticId = MoveTemp(Candidate);
    return true;
}

bool IsNullPlacementStateValue(const FString& Value)
{
    return Value.IsEmpty() || Value.Equals(TEXT("none"), ESearchCase::IgnoreCase) ||
        Value.Equals(TEXT("null"), ESearchCase::IgnoreCase);
}

bool IsUniqueStablePlacementAnchor(UWorld* World,
                                   const FString& SemanticId,
                                   const AActor* ExpectedOwner = nullptr)
{
    if (!IsValid(World) || !IsStablePlacementAnchorSemanticId(SemanticId))
    {
        return false;
    }
    const FName StableTag(*(FString(StableSemanticTagPrefix) + SemanticId));
    const int32 DelimiterIndex = SemanticId.Find(
        PlacementAnchorDelimiter, ESearchCase::CaseSensitive, ESearchDir::FromStart);
    const FName OwnerTag(*(FString(PlacementOwnerTagPrefix) +
        SemanticId.Left(DelimiterIndex)));
    const AActor* Match = nullptr;
    for (TActorIterator<AActor> It(World); It; ++It)
    {
        if (!It->ActorHasTag(StableTag) || !It->ActorHasTag(OwnerTag))
        {
            continue;
        }
        if (Match != nullptr)
        {
            return false;
        }
        Match = *It;
    }
    return IsValid(Match) && IsValid(Match->GetRootComponent()) &&
        (ExpectedOwner == nullptr || Match == ExpectedOwner);
}

bool StablePlacementAnchorSemanticId(const USceneComponent* PlacementAnchor,
                                     FString& OutSemanticId)
{
    if (!IsValid(PlacementAnchor))
    {
        return false;
    }
    const AActor* Owner = PlacementAnchor->GetOwner();
    if (!IsValid(Owner) || PlacementAnchor != Owner->GetRootComponent())
    {
        return false;
    }

    FString Match;
    for (const FName& Tag : Owner->Tags)
    {
        const FString TagValue = Tag.ToString();
        if (!TagValue.StartsWith(StableSemanticTagPrefix, ESearchCase::CaseSensitive))
        {
            continue;
        }
        const FString Candidate = TagValue.RightChop(FCString::Strlen(StableSemanticTagPrefix));
        if (!IsStablePlacementAnchorSemanticId(Candidate))
        {
            continue;
        }
        if (!Match.IsEmpty() && Match != Candidate)
        {
            return false;
        }
        Match = Candidate;
    }
    if (Match.IsEmpty() ||
        !IsUniqueStablePlacementAnchor(Owner->GetWorld(), Match, Owner))
    {
        return false;
    }
    OutSemanticId = MoveTemp(Match);
    return true;
}

bool NormalizeStoredPlacementAnchor(UWorld* World,
                                    const FString& Value,
                                    FString& OutSemanticId)
{
    return CanonicalizePlacementAnchorSemanticId(Value, OutSemanticId) &&
        IsUniqueStablePlacementAnchor(World, OutSemanticId);
}

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

void AVistaPickupActor::BeginPlay()
{
    Super::BeginPlay();
    NormalizePlacementState();
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
    if (IsValid(HeldBy))
    {
        State.Values.Remove(TEXT("placed_at"));
    }
    else if (const FString* PlacementValue = State.Values.Find(TEXT("placed_at")))
    {
        FString NormalizedPlacement;
        if (NormalizeStoredPlacementAnchor(GetWorld(), *PlacementValue, NormalizedPlacement))
        {
            State.Values.Add(TEXT("placed_at"), NormalizedPlacement);
        }
        else
        {
            State.Values.Remove(TEXT("placed_at"));
        }
    }
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

    FString NormalizedPlacement;
    const FString* PlacementValue = State.Values.Find(TEXT("placed_at"));
    const bool bRestorePlacement = PlacementValue &&
        !IsNullPlacementStateValue(*PlacementValue);
    if (bRestorePlacement &&
        !NormalizeStoredPlacementAnchor(GetWorld(), *PlacementValue, NormalizedPlacement))
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::NotFound,
            TEXT("BASELINE_PLACEMENT_ANCHOR_NOT_FOUND"), SemanticId);
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
    if (bRestorePlacement)
    {
        RuntimeStateValues.Add(TEXT("placed_at"), NormalizedPlacement);
    }
    else
    {
        RuntimeStateValues.Remove(TEXT("placed_at"));
    }
    Mesh->SetSimulatePhysics(bPortable && !bRestorePlacement);
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
    RuntimeStateValues.Remove(TEXT("placed_at"));
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

    FString PlacementAnchorSemanticId;
    if (IsValid(PlacementAnchor) &&
        !StablePlacementAnchorSemanticId(PlacementAnchor, PlacementAnchorSemanticId))
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::InvalidState,
            TEXT("PLACEMENT_ANCHOR_NOT_STABLE"), SemanticId);
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
        RuntimeStateValues.Remove(TEXT("placed_at"));
    }
    else
    {
        RuntimeStateValues.Add(TEXT("placed_at"), PlacementAnchorSemanticId);
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

void AVistaPickupActor::NormalizePlacementState()
{
    const FString* PlacementValue = RuntimeStateValues.Find(TEXT("placed_at"));
    if (!PlacementValue || IsNullPlacementStateValue(*PlacementValue))
    {
        RuntimeStateValues.Remove(TEXT("placed_at"));
        return;
    }
    FString NormalizedPlacement;
    if (NormalizeStoredPlacementAnchor(GetWorld(), *PlacementValue, NormalizedPlacement))
    {
        RuntimeStateValues.Add(TEXT("placed_at"), NormalizedPlacement);
        Mesh->SetSimulatePhysics(false);
    }
    else
    {
        RuntimeStateValues.Remove(TEXT("placed_at"));
    }
}
