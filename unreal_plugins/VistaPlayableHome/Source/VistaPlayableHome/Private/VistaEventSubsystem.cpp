#include "VistaEventSubsystem.h"

#include "EngineUtils.h"
#include "VistaHomeNpcCharacter.h"
#include "VistaHomeNpcController.h"
#include "VistaInteractable.h"
#include "VistaPickupActor.h"
#include "VistaPlayableHomeCharacter.h"
#include "VistaSemanticActor.h"

void UVistaEventSubsystem::InitializeWorldRevision(FName Revision)
{
    if (Revision.IsNone())
    {
        return;
    }
    if (!ActiveEventId.IsNone())
    {
        RestoreBaseline();
    }
    WorldRevision = Revision;
    // The Node broker opens every UE session at generation zero.  World
    // initialization is not a command and must not consume that first value.
    SessionGeneration = 0;
    EventStatus = EVistaEventStatus::Inactive;
}

bool UVistaEventSubsystem::RegisterEventDefinitions(
    const TArray<FVistaEventDefinition>& Definitions,
    FName& OutCode)
{
    TMap<FName, FVistaEventDefinition> Candidate;
    for (const FVistaEventDefinition& Definition : Definitions)
    {
        if (!ValidateDefinition(Definition, OutCode) || Candidate.Contains(Definition.EventId))
        {
            if (Candidate.Contains(Definition.EventId))
            {
                OutCode = TEXT("DUPLICATE_EVENT_ID");
            }
            return false;
        }
        Candidate.Add(Definition.EventId, Definition);
    }
    EventDefinitions = MoveTemp(Candidate);
    OutCode = TEXT("EVENTS_REGISTERED");
    return true;
}

bool UVistaEventSubsystem::ValidateDefinition(
    const FVistaEventDefinition& Definition,
    FName& OutCode) const
{
    if (Definition.EventId.IsNone() || Definition.CompatibleRevision.IsNone())
    {
        OutCode = TEXT("EVENT_IDENTITY_REQUIRED");
        return false;
    }
    if (!FMath::IsFinite(Definition.TimeoutSeconds) ||
        Definition.TimeoutSeconds < 0.1f || Definition.TimeoutSeconds > 3600.0f)
    {
        OutCode = TEXT("EVENT_TIMEOUT_INVALID");
        return false;
    }
    TSet<FName> OperationIds;
    for (const FVistaEventOperation& Operation : Definition.InitialOperations)
    {
        if (Operation.OperationId.IsNone() || OperationIds.Contains(Operation.OperationId))
        {
            OutCode = Operation.OperationId.IsNone()
                ? FName(TEXT("OPERATION_ID_REQUIRED"))
                : FName(TEXT("DUPLICATE_OPERATION_ID"));
            return false;
        }
        OperationIds.Add(Operation.OperationId);
        const bool bTargetOptional = Operation.Type == EVistaEventOperationType::SetGoal;
        if (!bTargetOptional && Operation.TargetSemanticId.IsEmpty())
        {
            OutCode = TEXT("OPERATION_TARGET_REQUIRED");
            return false;
        }
        if (Operation.Type == EVistaEventOperationType::SpawnFixture && !Operation.FixtureClass)
        {
            OutCode = TEXT("FIXTURE_CLASS_REQUIRED");
            return false;
        }
    }
    OutCode = TEXT("OK");
    return true;
}

bool UVistaEventSubsystem::ValidateEnvelope(
    FName ExpectedRevision,
    int32 ExpectedGeneration,
    FName& OutCode) const
{
    if (ExpectedRevision.IsNone() || ExpectedRevision != WorldRevision)
    {
        OutCode = TEXT("REVISION_MISMATCH");
        return false;
    }
    if (ExpectedGeneration != SessionGeneration)
    {
        OutCode = TEXT("SESSION_GENERATION_MISMATCH");
        return false;
    }
    return true;
}

bool UVistaEventSubsystem::CommitCommandGeneration(
    int32 ExpectedGeneration,
    int32& OutGeneration)
{
    if (ExpectedGeneration != SessionGeneration)
    {
        OutGeneration = SessionGeneration;
        return false;
    }
    ++SessionGeneration;
    OutGeneration = SessionGeneration;
    return true;
}

bool UVistaEventSubsystem::StartEvent(
    FName EventId,
    FName ExpectedRevision,
    int32 ExpectedGeneration,
    FName& OutCode)
{
    if (!ValidateEnvelope(ExpectedRevision, ExpectedGeneration, OutCode))
    {
        return false;
    }
    if (!ActiveEventId.IsNone())
    {
        OutCode = ActiveEventId == EventId ? FName(TEXT("EVENT_ALREADY_ACTIVE"))
                                           : FName(TEXT("ANOTHER_EVENT_ACTIVE"));
        return false;
    }
    const FVistaEventDefinition* Definition = EventDefinitions.Find(EventId);
    if (!Definition)
    {
        OutCode = TEXT("EVENT_NOT_REGISTERED");
        return false;
    }
    if (Definition->CompatibleRevision != WorldRevision)
    {
        OutCode = TEXT("EVENT_REVISION_INCOMPATIBLE");
        return false;
    }

    // Validate every target before applying any operation. This makes target
    // absence a side-effect-free failure rather than a partial overlay.
    for (const FVistaEventOperation& Operation : Definition->InitialOperations)
    {
        if (Operation.Type == EVistaEventOperationType::SetGoal)
        {
            continue;
        }
        if (Operation.Type != EVistaEventOperationType::SpawnFixture &&
            !ResolveSemanticActor(Operation.TargetSemanticId))
        {
            OutCode = TEXT("EVENT_TARGET_NOT_FOUND");
            return false;
        }
    }

    EventStatus = EVistaEventStatus::Applying;
    BaselineStates.Reset();
    SpawnedFixtures.Reset();
    ModifiedNpcControllers.Reset();
    ActivePublicGoal = Definition->PublicGoal;

    // Reset is a world-overlay rollback, not merely an inverse of the
    // initial operations. Snapshot every resettable semantic actor plus the
    // player before accepting the event so later live interactions are also
    // restored.
    for (TActorIterator<AActor> It(GetWorld()); It; ++It)
    {
        AActor* Actor = *It;
        FVistaEntityRuntimeState Snapshot;
        if (Actor->GetClass()->ImplementsInterface(UVistaInteractable::StaticClass()))
        {
            Snapshot = IVistaInteractable::Execute_VistaGetRuntimeState(Actor);
        }
        else if (const AVistaPlayableHomeCharacter* Player =
                     Cast<AVistaPlayableHomeCharacter>(Actor))
        {
            Snapshot.SemanticId = Player->SemanticId;
            Snapshot.Transform = Player->GetActorTransform();
            Snapshot.bHidden = Player->IsHidden();
        }
        if (!Snapshot.SemanticId.IsEmpty())
        {
            BaselineStates.Add(Actor, Snapshot);
        }
    }

    for (const FVistaEventOperation& Operation : Definition->InitialOperations)
    {
        AActor* Target = Operation.Type == EVistaEventOperationType::SetGoal ||
                         Operation.Type == EVistaEventOperationType::SpawnFixture
            ? nullptr
            : ResolveSemanticActor(Operation.TargetSemanticId);
        if (IsValid(Target) && !BaselineStates.Contains(Target))
        {
            FVistaEntityRuntimeState Snapshot;
            Snapshot.SemanticId = Operation.TargetSemanticId;
            Snapshot.Transform = Target->GetActorTransform();
            Snapshot.bHidden = Target->IsHidden();
            if (Target->GetClass()->ImplementsInterface(UVistaInteractable::StaticClass()))
            {
                Snapshot = IVistaInteractable::Execute_VistaGetRuntimeState(Target);
            }
            BaselineStates.Add(Target, Snapshot);
        }
        if (!ApplyOperation(Operation, OutCode))
        {
            RestoreBaseline();
            EventStatus = EVistaEventStatus::Failed;
            return false;
        }
    }

    ActiveEventId = EventId;
    ActiveTimeoutSeconds = Definition->TimeoutSeconds;
    EventStartedAt = GetWorld()->GetTimeSeconds();
    EventStatus = EVistaEventStatus::Active;
    OutCode = TEXT("EVENT_STARTED");
    return true;
}

bool UVistaEventSubsystem::ApplyOperation(
    const FVistaEventOperation& Operation,
    FName& OutCode)
{
    if (Operation.Type == EVistaEventOperationType::SetGoal)
    {
        OutCode = TEXT("GOAL_SET");
        return true;
    }
    if (Operation.Type == EVistaEventOperationType::SpawnFixture)
    {
        if (ResolveSemanticActor(Operation.TargetSemanticId))
        {
            OutCode = TEXT("FIXTURE_ID_ALREADY_EXISTS");
            return false;
        }
        FActorSpawnParameters Params;
        Params.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AlwaysSpawn;
        AActor* Spawned = GetWorld()->SpawnActor<AActor>(
            Operation.FixtureClass, Operation.Transform, Params);
        if (!IsValid(Spawned))
        {
            OutCode = TEXT("FIXTURE_SPAWN_FAILED");
            return false;
        }
        if (AVistaSemanticActor* SemanticActor = Cast<AVistaSemanticActor>(Spawned))
        {
            SemanticActor->SemanticId = Operation.TargetSemanticId;
            SemanticActor->WorldRevision = WorldRevision;
        }
        Spawned->Tags.AddUnique(FName(*Operation.TargetSemanticId));
        SpawnedFixtures.Add(Spawned);
        OutCode = TEXT("FIXTURE_SPAWNED");
        return true;
    }

    AActor* Target = ResolveSemanticActor(Operation.TargetSemanticId);
    if (!IsValid(Target))
    {
        OutCode = TEXT("EVENT_TARGET_NOT_FOUND");
        return false;
    }
    if (Operation.Type == EVistaEventOperationType::SetTransform)
    {
        Target->SetActorTransform(Operation.Transform, false, nullptr, ETeleportType::TeleportPhysics);
        OutCode = TEXT("TRANSFORM_SET");
        return true;
    }
    if (Operation.Type == EVistaEventOperationType::SetVisibility)
    {
        if (!Target->GetClass()->ImplementsInterface(UVistaInteractable::StaticClass()))
        {
            Target->SetActorHiddenInGame(!Operation.bBooleanValue);
            OutCode = TEXT("VISIBILITY_SET");
            return true;
        }
        FVistaEntityRuntimeState State =
            IVistaInteractable::Execute_VistaGetRuntimeState(Target);
        State.bHidden = !Operation.bBooleanValue;
        State.Values.Add(TEXT("visible"), Operation.bBooleanValue ? TEXT("true") : TEXT("false"));
        const FVistaInteractionResult Result =
            IVistaInteractable::Execute_VistaApplyRuntimeState(Target, State);
        OutCode = Result.Code;
        return Result.IsSuccess();
    }
    if (Operation.Type == EVistaEventOperationType::SetNpcQueue)
    {
        AVistaHomeNpcCharacter* Npc = Cast<AVistaHomeNpcCharacter>(Target);
        AVistaHomeNpcController* Controller = IsValid(Npc)
            ? Cast<AVistaHomeNpcController>(Npc->GetController())
            : nullptr;
        if (!IsValid(Controller) || !Controller->ReplaceActionQueue(Operation.NpcActions, OutCode))
        {
            if (OutCode.IsNone())
            {
                OutCode = TEXT("NPC_CONTROLLER_UNAVAILABLE");
            }
            return false;
        }
        ModifiedNpcControllers.AddUnique(Controller);
        return true;
    }
    if (!Target->GetClass()->ImplementsInterface(UVistaInteractable::StaticClass()))
    {
        OutCode = TEXT("TARGET_NOT_STATEFUL");
        return false;
    }
    FVistaEntityRuntimeState State = IVistaInteractable::Execute_VistaGetRuntimeState(Target);
    if (Operation.Type == EVistaEventOperationType::SetState)
    {
        for (const TPair<FName, FString>& Pair : Operation.StateValues)
        {
            State.Values.Add(Pair.Key, Pair.Value);
        }
    }
    else if (Operation.Type == EVistaEventOperationType::SetPortable)
    {
        State.bPortable = Operation.bBooleanValue;
    }
    else
    {
        OutCode = TEXT("EVENT_OPERATION_UNSUPPORTED");
        return false;
    }
    const FVistaInteractionResult Result =
        IVistaInteractable::Execute_VistaApplyRuntimeState(Target, State);
    OutCode = Result.Code;
    return Result.IsSuccess();
}

bool UVistaEventSubsystem::ResetEvent(
    FName ExpectedRevision,
    int32 ExpectedGeneration,
    FName& OutCode)
{
    if (!ValidateEnvelope(ExpectedRevision, ExpectedGeneration, OutCode))
    {
        return false;
    }
    if (ActiveEventId.IsNone() && BaselineStates.IsEmpty() && SpawnedFixtures.IsEmpty())
    {
        OutCode = TEXT("NO_ACTIVE_EVENT");
        return false;
    }
    EventStatus = EVistaEventStatus::Resetting;
    RestoreBaseline();
    EventStatus = EVistaEventStatus::Inactive;
    OutCode = TEXT("EVENT_RESET");
    return true;
}

void UVistaEventSubsystem::RestoreBaseline()
{
    for (const TWeakObjectPtr<AVistaHomeNpcController>& Controller : ModifiedNpcControllers)
    {
        if (Controller.IsValid())
        {
            Controller->CancelActionQueue(TEXT("EVENT_RESET"));
        }
    }
    // Release all currently carried pickups first. This prevents map iteration
    // order from making a baseline-held item fail to reclaim its carrier slot.
    for (const TPair<TWeakObjectPtr<AActor>, FVistaEntityRuntimeState>& Pair : BaselineStates)
    {
        if (AVistaPickupActor* Pickup = Cast<AVistaPickupActor>(Pair.Key.Get());
            IsValid(Pickup) && IsValid(Pickup->GetCarrier()))
        {
            Pickup->ReleaseFromCarrier();
        }
    }
    for (const TPair<TWeakObjectPtr<AActor>, FVistaEntityRuntimeState>& Pair : BaselineStates)
    {
        AActor* Actor = Pair.Key.Get();
        if (!IsValid(Actor))
        {
            continue;
        }
        if (Actor->GetClass()->ImplementsInterface(UVistaInteractable::StaticClass()))
        {
            IVistaInteractable::Execute_VistaApplyRuntimeState(Actor, Pair.Value);
        }
        else
        {
            Actor->SetActorTransform(Pair.Value.Transform, false, nullptr,
                                     ETeleportType::TeleportPhysics);
            Actor->SetActorHiddenInGame(Pair.Value.bHidden);
        }
    }
    for (const TWeakObjectPtr<AActor>& Spawned : SpawnedFixtures)
    {
        if (Spawned.IsValid())
        {
            Spawned->Destroy();
        }
    }
    BaselineStates.Reset();
    SpawnedFixtures.Reset();
    ModifiedNpcControllers.Reset();
    ActiveEventId = NAME_None;
    ActivePublicGoal.Reset();
    ActiveTimeoutSeconds = 0.0f;
}

AActor* UVistaEventSubsystem::ResolveSemanticActor(const FString& SemanticId) const
{
    if (SemanticId.IsEmpty() || !GetWorld())
    {
        return nullptr;
    }
    const FName Tag(*SemanticId);
    const FName StableTag(*(FString(TEXT("VistaSemanticId=")) + SemanticId));
    for (TActorIterator<AActor> It(GetWorld()); It; ++It)
    {
        if (It->ActorHasTag(Tag) || It->ActorHasTag(StableTag))
        {
            return *It;
        }
    }
    return nullptr;
}

void UVistaEventSubsystem::Tick(float DeltaTime)
{
    if (EventStatus == EVistaEventStatus::Active &&
        GetWorld()->GetTimeSeconds() - EventStartedAt > ActiveTimeoutSeconds)
    {
        EventStatus = EVistaEventStatus::TimedOut;
    }
}

TStatId UVistaEventSubsystem::GetStatId() const
{
    RETURN_QUICK_DECLARE_CYCLE_STAT(UVistaEventSubsystem, STATGROUP_Tickables);
}
