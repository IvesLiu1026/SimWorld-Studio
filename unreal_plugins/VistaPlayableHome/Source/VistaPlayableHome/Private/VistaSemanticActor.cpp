#include "VistaSemanticActor.h"

AVistaSemanticActor::AVistaSemanticActor()
{
    bReplicates = true;
    SetReplicateMovement(true);
    AllowedAffordances = {EVistaAffordance::Inspect};
}

void AVistaSemanticActor::BeginPlay()
{
    Super::BeginPlay();
    if (!SemanticId.IsEmpty())
    {
        Tags.AddUnique(FName(*SemanticId));
    }
    RuntimeStateValues = InitialStateValues;
    const FString* VisibleValue = RuntimeStateValues.Find(TEXT("visible"));
    const bool bVisible = !VisibleValue ||
        !VisibleValue->Equals(TEXT("false"), ESearchCase::IgnoreCase);
    SetActorHiddenInGame(!bVisible);
    RuntimeStateValues.Add(TEXT("visible"), bVisible ? TEXT("true") : TEXT("false"));
}

FString AVistaSemanticActor::VistaGetSemanticId_Implementation() const
{
    return SemanticId;
}

TArray<EVistaAffordance> AVistaSemanticActor::VistaGetAffordances_Implementation() const
{
    return AllowedAffordances;
}

FVistaEntityRuntimeState AVistaSemanticActor::VistaGetRuntimeState_Implementation() const
{
    FVistaEntityRuntimeState State;
    State.SemanticId = SemanticId;
    State.Transform = GetActorTransform();
    State.bHidden = IsHidden();
    State.Values = RuntimeStateValues;
    State.Values.Add(TEXT("visible"), State.bHidden ? TEXT("false") : TEXT("true"));
    return State;
}

FVistaInteractionResult AVistaSemanticActor::VistaApplyRuntimeState_Implementation(
    const FVistaEntityRuntimeState& State)
{
    if (!State.SemanticId.IsEmpty() && State.SemanticId != SemanticId)
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::NotFound, TEXT("SEMANTIC_ID_MISMATCH"), SemanticId);
    }

    SetActorTransform(State.Transform, false, nullptr, ETeleportType::TeleportPhysics);
    RuntimeStateValues = State.Values;
    const FString* VisibleValue = RuntimeStateValues.Find(TEXT("visible"));
    const bool bHidden = VisibleValue
        ? VisibleValue->Equals(TEXT("false"), ESearchCase::IgnoreCase)
        : State.bHidden;
    SetActorHiddenInGame(bHidden);
    RuntimeStateValues.Add(TEXT("visible"), bHidden ? TEXT("false") : TEXT("true"));
    return FVistaInteractionResult::Success(
        SemanticId, VistaGetRuntimeState_Implementation(), TEXT("STATE_APPLIED"));
}

FVistaInteractionResult AVistaSemanticActor::VistaInteract_Implementation(
    const FVistaInteractionRequest& Request)
{
    const FVistaInteractionResult Validation = ValidateRequest(Request);
    if (!Validation.IsSuccess())
    {
        return Validation;
    }
    return FVistaInteractionResult::Success(
        SemanticId, VistaGetRuntimeState_Implementation(), TEXT("INSPECTED"));
}

bool AVistaSemanticActor::Supports(EVistaAffordance Affordance) const
{
    return AllowedAffordances.Contains(Affordance);
}

FVistaInteractionResult AVistaSemanticActor::ValidateRequest(
    const FVistaInteractionRequest& Request) const
{
    if (!IsValid(Request.Requester))
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::InvalidRequester, TEXT("REQUESTER_REQUIRED"), SemanticId);
    }
    if (!Request.ExpectedRevision.IsNone() && Request.ExpectedRevision != WorldRevision)
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::RevisionMismatch, TEXT("REVISION_MISMATCH"), SemanticId);
    }
    if (!Supports(Request.Affordance))
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::Unsupported, TEXT("AFFORDANCE_UNSUPPORTED"), SemanticId);
    }
    return FVistaInteractionResult::Success(
        SemanticId, VistaGetRuntimeState_Implementation(), TEXT("VALID"));
}
