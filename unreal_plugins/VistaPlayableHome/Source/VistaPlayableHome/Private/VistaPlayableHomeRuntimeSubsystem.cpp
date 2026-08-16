#include "VistaPlayableHomeRuntimeSubsystem.h"

#include "EngineUtils.h"
#include "DynamicRHI.h"
#include "HAL/IConsoleManager.h"
#include "Misc/EngineVersion.h"
#include "RHI.h"
#include "RHIShaderPlatform.h"
#include "RHIStrings.h"
#include "VistaEventSubsystem.h"
#include "VistaHomeNpcCharacter.h"
#include "VistaHomeNpcController.h"
#include "VistaInteractable.h"

namespace
{
constexpr const TCHAR* RendererCVarNames[] = {
    TEXT("r.DynamicGlobalIlluminationMethod"),
    TEXT("r.ReflectionMethod"),
    TEXT("r.Shadow.Virtual.Enable"),
    TEXT("r.AntiAliasingMethod"),
    TEXT("r.Nanite"),
    TEXT("r.GenerateMeshDistanceFields"),
    TEXT("r.DefaultFeature.AutoExposure.ExtendDefaultLuminanceRange"),
    TEXT("r.EyeAdaptation.PreExposureOverride"),
    TEXT("r.RayTracing"),
    TEXT("r.Lumen.HardwareRayTracing"),
    TEXT("r.ScreenPercentage"),
    TEXT("r.Streaming.PoolSize"),
    TEXT("sg.ViewDistanceQuality"),
    TEXT("sg.AntiAliasingQuality"),
    TEXT("sg.ShadowQuality"),
    TEXT("sg.GlobalIlluminationQuality"),
    TEXT("sg.ReflectionQuality"),
    TEXT("sg.PostProcessQuality"),
    TEXT("sg.TextureQuality"),
    TEXT("sg.EffectsQuality"),
    TEXT("sg.FoliageQuality"),
    TEXT("sg.ShadingQuality"),
};
} // namespace

FVistaLiveCommandResult UVistaPlayableHomeRuntimeSubsystem::GetStatus(
    FName CommandId) const
{
    FVistaLiveCommandResult Output;
    Output.CommandId = CommandId;
    const UVistaEventSubsystem* Events = GetWorld()->GetSubsystem<UVistaEventSubsystem>();
    if (!IsValid(Events))
    {
        Output.Code = TEXT("EVENT_SUBSYSTEM_UNAVAILABLE");
        return Output;
    }
    Output.WorldRevision = Events->GetWorldRevision();
    Output.SessionGeneration = Events->GetSessionGeneration();
    Output.EventStatus = Events->GetEventStatus();
    Output.ActiveEventId = Events->GetActiveEventId();
    Output.bSucceeded = !Output.WorldRevision.IsNone();
    Output.Code = Output.bSucceeded ? FName(TEXT("READY"))
                                   : FName(TEXT("WORLD_NOT_INITIALIZED"));
    return Output;
}

FVistaRendererStatusResult
UVistaPlayableHomeRuntimeSubsystem::GetRendererStatus(FName CommandId) const
{
    FVistaRendererStatusResult Output;
    Output.CommandId = CommandId;
    if (CommandId.IsNone())
    {
        Output.Code = TEXT("COMMAND_ID_REQUIRED");
        return Output;
    }

    Output.Observation.UnrealEngineVersion = FEngineVersion::Current().ToString();
    Output.Observation.Rhi = GDynamicRHI ? FString(GDynamicRHI->GetName()) : FString();
    GetFeatureLevelName(
        GMaxRHIFeatureLevel, Output.Observation.FeatureLevel);
    Output.Observation.ShaderPlatform =
        LegacyShaderPlatformToShaderFormat(GMaxRHIShaderPlatform).ToString();
    if (Output.Observation.UnrealEngineVersion.IsEmpty() ||
        Output.Observation.Rhi.IsEmpty() ||
        Output.Observation.FeatureLevel.IsEmpty() ||
        Output.Observation.ShaderPlatform.IsEmpty())
    {
        Output.Code = TEXT("RENDERER_IDENTITY_UNAVAILABLE");
        return Output;
    }

    for (const TCHAR* Name : RendererCVarNames)
    {
        const IConsoleVariable* Variable =
            IConsoleManager::Get().FindConsoleVariable(Name);
        if (Variable == nullptr)
        {
            Output.Code = TEXT("RENDERER_CVAR_UNAVAILABLE");
            Output.Observation.ConsoleVariables.Reset();
            return Output;
        }
        const double Value = static_cast<double>(Variable->GetFloat());
        if (!FMath::IsFinite(Value))
        {
            Output.Code = TEXT("RENDERER_CVAR_NONFINITE");
            Output.Observation.ConsoleVariables.Reset();
            return Output;
        }
        Output.Observation.ConsoleVariables.Add(Name, Value);
    }

    Output.bSucceeded = true;
    Output.Code = TEXT("RENDERER_STATUS_OBSERVED");
    return Output;
}

bool UVistaPlayableHomeRuntimeSubsystem::ValidateEnvelope(
    const FVistaLiveCommandEnvelope& Envelope,
    FVistaLiveCommandResult& OutResult) const
{
    OutResult.CommandId = Envelope.CommandId;
    if (Envelope.CommandId.IsNone())
    {
        OutResult.Code = TEXT("COMMAND_ID_REQUIRED");
        return false;
    }
    const UVistaEventSubsystem* Events = GetWorld()->GetSubsystem<UVistaEventSubsystem>();
    if (!IsValid(Events))
    {
        OutResult.Code = TEXT("EVENT_SUBSYSTEM_UNAVAILABLE");
        return false;
    }
    OutResult.SessionGeneration = Events->GetSessionGeneration();
    OutResult.EventStatus = Events->GetEventStatus();
    OutResult.WorldRevision = Events->GetWorldRevision();
    OutResult.ActiveEventId = Events->GetActiveEventId();
    if (Envelope.ExpectedRevision.IsNone() ||
        Envelope.ExpectedRevision != Events->GetWorldRevision())
    {
        OutResult.Code = TEXT("REVISION_MISMATCH");
        return false;
    }
    if (Envelope.SessionGeneration != Events->GetSessionGeneration())
    {
        OutResult.Code = TEXT("SESSION_GENERATION_MISMATCH");
        return false;
    }
    return true;
}

FVistaLiveCommandResult UVistaPlayableHomeRuntimeSubsystem::ExecuteInteraction(
    const FVistaLiveInteractionCommand& Command)
{
    FVistaLiveCommandResult Output;
    Output.TargetSemanticId = Command.TargetSemanticId;
    if (!ValidateEnvelope(Command.Envelope, Output))
    {
        return Output;
    }
    AActor* Requester = ResolveSemanticActor(Command.RequesterSemanticId);
    AActor* Target = ResolveSemanticActor(Command.TargetSemanticId);
    if (!IsValid(Requester))
    {
        Output.Code = TEXT("REQUESTER_NOT_FOUND");
        return Output;
    }
    if (!IsValid(Target) ||
        !Target->GetClass()->ImplementsInterface(UVistaInteractable::StaticClass()))
    {
        Output.Code = TEXT("TARGET_NOT_INTERACTABLE");
        return Output;
    }

    USceneComponent* PlacementAnchor = nullptr;
    if (!Command.PlacementAnchorSemanticId.IsEmpty())
    {
        AActor* AnchorActor = ResolveSemanticActor(Command.PlacementAnchorSemanticId);
        if (!IsValid(AnchorActor))
        {
            Output.Code = TEXT("PLACEMENT_ANCHOR_NOT_FOUND");
            return Output;
        }
        PlacementAnchor = AnchorActor->GetRootComponent();
    }

    FVistaInteractionRequest Request;
    Request.Requester = Requester;
    Request.Affordance = Command.Affordance;
    Request.PlacementAnchor = PlacementAnchor;
    Request.ExpectedRevision = Command.Envelope.ExpectedRevision;
    Request.SessionGeneration = Command.Envelope.SessionGeneration;
    const FVistaInteractionResult Result =
        IVistaInteractable::Execute_VistaInteract(Target, Request);
    Output.bSucceeded = Result.IsSuccess();
    Output.Code = Result.Code;
    Output.State = Result.State;
    if (Output.bSucceeded)
    {
        UVistaEventSubsystem* Events = GetWorld()->GetSubsystem<UVistaEventSubsystem>();
        if (!Events->CommitCommandGeneration(
                Command.Envelope.SessionGeneration, Output.SessionGeneration))
        {
            Output.bSucceeded = false;
            Output.Code = TEXT("SESSION_GENERATION_COMMIT_FAILED");
        }
    }
    return Output;
}

FVistaLiveCommandResult UVistaPlayableHomeRuntimeSubsystem::ExecuteNpcQueue(
    const FVistaLiveNpcQueueCommand& Command)
{
    FVistaLiveCommandResult Output;
    Output.TargetSemanticId = Command.NpcSemanticId;
    if (!ValidateEnvelope(Command.Envelope, Output))
    {
        return Output;
    }
    AVistaHomeNpcCharacter* Npc =
        Cast<AVistaHomeNpcCharacter>(ResolveSemanticActor(Command.NpcSemanticId));
    AVistaHomeNpcController* Controller = IsValid(Npc)
        ? Cast<AVistaHomeNpcController>(Npc->GetController())
        : nullptr;
    if (!IsValid(Controller))
    {
        Output.Code = TEXT("NPC_CONTROLLER_NOT_FOUND");
        return Output;
    }
    if (!Command.bReplace)
    {
        Output.Code = TEXT("QUEUE_REPLACE_REQUIRED");
        return Output;
    }
    FName Code;
    Output.bSucceeded = Controller->ReplaceActionQueue(Command.Actions, Code);
    Output.Code = Code;
    if (Output.bSucceeded)
    {
        UVistaEventSubsystem* Events = GetWorld()->GetSubsystem<UVistaEventSubsystem>();
        if (!Events->CommitCommandGeneration(
                Command.Envelope.SessionGeneration, Output.SessionGeneration))
        {
            Output.bSucceeded = false;
            Output.Code = TEXT("SESSION_GENERATION_COMMIT_FAILED");
        }
    }
    return Output;
}

FVistaLiveCommandResult UVistaPlayableHomeRuntimeSubsystem::ExecuteEvent(
    const FVistaLiveEventCommand& Command)
{
    FVistaLiveCommandResult Output;
    if (!ValidateEnvelope(Command.Envelope, Output))
    {
        return Output;
    }
    UVistaEventSubsystem* Events = GetWorld()->GetSubsystem<UVistaEventSubsystem>();
    FName Code;
    if (Command.Operation == EVistaLiveEventOperation::StartEvent)
    {
        if (Command.EventId.IsNone())
        {
            Output.Code = TEXT("EVENT_ID_REQUIRED");
            return Output;
        }
        Output.bSucceeded = Events->StartEvent(
            Command.EventId, Command.Envelope.ExpectedRevision,
            Command.Envelope.SessionGeneration, Code);
    }
    else
    {
        Output.bSucceeded = Events->ResetEvent(
            Command.Envelope.ExpectedRevision,
            Command.Envelope.SessionGeneration, Code);
    }
    Output.Code = Code;
    Output.EventStatus = Events->GetEventStatus();
    Output.ActiveEventId = Events->GetActiveEventId();
    if (Output.bSucceeded && !Events->CommitCommandGeneration(
            Command.Envelope.SessionGeneration, Output.SessionGeneration))
    {
        Output.bSucceeded = false;
        Output.Code = TEXT("SESSION_GENERATION_COMMIT_FAILED");
    }
    else if (!Output.bSucceeded)
    {
        Output.SessionGeneration = Events->GetSessionGeneration();
    }
    return Output;
}

AActor* UVistaPlayableHomeRuntimeSubsystem::ResolveSemanticActor(
    const FString& SemanticId) const
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
