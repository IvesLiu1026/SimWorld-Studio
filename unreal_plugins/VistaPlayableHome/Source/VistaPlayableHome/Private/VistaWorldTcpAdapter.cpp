#include "VistaWorldTcpAdapter.h"

#include "Async/Async.h"
#include "Common/TcpListener.h"
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Engine/Engine.h"
#include "Engine/World.h"
#include "HAL/PlatformProcess.h"
#include "HAL/PlatformTime.h"
#include "Interfaces/IPv4/IPv4Endpoint.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "SocketSubsystem.h"
#include "Sockets.h"
#include "VistaPlayableHomeRuntimeSubsystem.h"

namespace
{
constexpr int32 MaxRequestBytes = 64 * 1024;
constexpr int32 MaxResponseBytes = 64 * 1024;
constexpr double ReadTimeoutSeconds = 2.0;
constexpr uint32 DispatchTimeoutMilliseconds = 10 * 1000;

struct FDispatchState final
{
    FDispatchState() : Completed(FPlatformProcess::GetSynchEventFromPool(true)) {}
    ~FDispatchState() { FPlatformProcess::ReturnSynchEventToPool(Completed); }

    FEvent* Completed = nullptr;
    FCriticalSection Mutex;
    bool bCanceled = false;
    FString Response;
};

TSet<FString> KeySet(std::initializer_list<const TCHAR*> Values)
{
    TSet<FString> Result;
    for (const TCHAR* Value : Values)
    {
        Result.Add(Value);
    }
    return Result;
}

bool ExactKeys(const TSharedPtr<FJsonObject>& Object,
               const TSet<FString>& Required,
               const TSet<FString>& Optional)
{
    if (!Object.IsValid())
    {
        return false;
    }
    for (const TPair<FString, TSharedPtr<FJsonValue>>& Pair : Object->Values)
    {
        if (!Required.Contains(Pair.Key) && !Optional.Contains(Pair.Key))
        {
            return false;
        }
    }
    for (const FString& Key : Required)
    {
        if (!Object->Values.Contains(Key))
        {
            return false;
        }
    }
    return Object->Values.Num() >= Required.Num() &&
           Object->Values.Num() <= Required.Num() + Optional.Num();
}

bool IsCommandId(const FString& Value)
{
    if (Value.Len() != 28 || !Value.StartsWith(TEXT("vwc-")))
    {
        return false;
    }
    for (int32 Index = 4; Index < Value.Len(); ++Index)
    {
        if (!FChar::IsHexDigit(Value[Index]) || FChar::IsUpper(Value[Index]))
        {
            return false;
        }
    }
    return true;
}

bool IsRevision(const FString& Value)
{
    if (Value.IsEmpty() || Value.Len() > 80)
    {
        return false;
    }
    for (const TCHAR Character : Value)
    {
        if (!((Character >= TEXT('a') && Character <= TEXT('z')) ||
              (Character >= TEXT('0') && Character <= TEXT('9')) ||
              Character == TEXT('.') || Character == TEXT('_') || Character == TEXT('-')))
        {
            return false;
        }
    }
    return true;
}

bool IsAsciiAlnum(const TCHAR Character)
{
    return (Character >= TEXT('A') && Character <= TEXT('Z')) ||
           (Character >= TEXT('a') && Character <= TEXT('z')) ||
           (Character >= TEXT('0') && Character <= TEXT('9'));
}

bool IsActionId(const FString& Value)
{
    if (Value.IsEmpty() || Value.Len() > 80 || !IsAsciiAlnum(Value[0]))
    {
        return false;
    }
    for (const TCHAR Character : Value)
    {
        if (!(IsAsciiAlnum(Character) || Character == TEXT('.') ||
              Character == TEXT('_') || Character == TEXT('-')))
        {
            return false;
        }
    }
    return true;
}

bool IsSemanticId(const FString& Value)
{
    if (Value.IsEmpty() || Value.Len() > 240 || !IsAsciiAlnum(Value[0]))
    {
        return false;
    }
    for (const TCHAR Character : Value)
    {
        if (!(IsAsciiAlnum(Character) || Character == TEXT('.') ||
              Character == TEXT('_') || Character == TEXT('/') || Character == TEXT('-')))
        {
            return false;
        }
    }
    return true;
}

bool ReadString(const TSharedPtr<FJsonObject>& Object,
                const TCHAR* Field,
                FString& Output)
{
    return Object->TryGetStringField(Field, Output);
}

bool ReadGeneration(const TSharedPtr<FJsonObject>& Object, int32& Output)
{
    double Number = 0.0;
    if (!Object->TryGetNumberField(TEXT("session_generation"), Number) ||
        !FMath::IsFinite(Number) || Number < 0.0 || Number > MAX_int32 ||
        FMath::FloorToDouble(Number) != Number)
    {
        return false;
    }
    Output = static_cast<int32>(Number);
    return true;
}

FString SerializeObject(const TSharedRef<FJsonObject>& Object)
{
    FString Output;
    const TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Output);
    FJsonSerializer::Serialize(Object, Writer);
    return Output;
}

FString ErrorResponse(const FString& CommandId, const TCHAR* Code)
{
    const TSharedRef<FJsonObject> Response = MakeShared<FJsonObject>();
    Response->SetStringField(TEXT("command_id"), CommandId);
    Response->SetStringField(TEXT("status"), TEXT("error"));
    Response->SetStringField(TEXT("code"), Code);
    return SerializeObject(Response);
}

TSharedRef<FJsonObject> RuntimeStateJson(const FVistaEntityRuntimeState& State)
{
    const TSharedRef<FJsonObject> Output = MakeShared<FJsonObject>();
    Output->SetStringField(TEXT("semantic_id"), State.SemanticId);
    Output->SetBoolField(TEXT("hidden"), State.bHidden);
    Output->SetBoolField(TEXT("portable"), State.bPortable);
    const FVector Location = State.Transform.GetLocation();
    const FRotator Rotation = State.Transform.Rotator();
    const FVector Scale = State.Transform.GetScale3D();
    const TSharedRef<FJsonObject> Transform = MakeShared<FJsonObject>();
    Transform->SetArrayField(TEXT("location_cm"), {
        MakeShared<FJsonValueNumber>(Location.X),
        MakeShared<FJsonValueNumber>(Location.Y),
        MakeShared<FJsonValueNumber>(Location.Z)});
    Transform->SetArrayField(TEXT("rotation_deg"), {
        // The public HouseSpec/World API contract is XYZ Euler. FRotator names
        // those same axis rotations Roll(X), Pitch(Y), and Yaw(Z).
        MakeShared<FJsonValueNumber>(Rotation.Roll),
        MakeShared<FJsonValueNumber>(Rotation.Pitch),
        MakeShared<FJsonValueNumber>(Rotation.Yaw)});
    Transform->SetArrayField(TEXT("scale"), {
        MakeShared<FJsonValueNumber>(Scale.X),
        MakeShared<FJsonValueNumber>(Scale.Y),
        MakeShared<FJsonValueNumber>(Scale.Z)});
    Output->SetObjectField(TEXT("transform"), Transform);
    const TSharedRef<FJsonObject> Values = MakeShared<FJsonObject>();
    for (const TPair<FName, FString>& Pair : State.Values)
    {
        Values->SetStringField(Pair.Key.ToString(), Pair.Value);
    }
    Output->SetObjectField(TEXT("values"), Values);
    return Output;
}

FString ResultResponse(const FVistaLiveCommandResult& Result,
                       bool bIncludeAuthoritativeStatus = false)
{
    const TSharedRef<FJsonObject> Response = MakeShared<FJsonObject>();
    Response->SetStringField(TEXT("command_id"), Result.CommandId.ToString());
    Response->SetStringField(TEXT("status"), Result.bSucceeded ? TEXT("success") : TEXT("error"));
    Response->SetStringField(TEXT("code"), Result.Code.ToString());
    Response->SetNumberField(TEXT("session_generation"), Result.SessionGeneration);
    if (bIncludeAuthoritativeStatus)
    {
        Response->SetStringField(TEXT("world_revision"), Result.WorldRevision.ToString());
        const TCHAR* EventStatus = TEXT("inactive");
        switch (Result.EventStatus)
        {
        case EVistaEventStatus::Applying: EventStatus = TEXT("applying"); break;
        case EVistaEventStatus::Active: EventStatus = TEXT("active"); break;
        case EVistaEventStatus::Succeeded: EventStatus = TEXT("succeeded"); break;
        case EVistaEventStatus::Failed: EventStatus = TEXT("failed"); break;
        case EVistaEventStatus::TimedOut: EventStatus = TEXT("timed_out"); break;
        case EVistaEventStatus::Resetting: EventStatus = TEXT("resetting"); break;
        default: break;
        }
        Response->SetStringField(TEXT("event_status"), EventStatus);
        if (Result.ActiveEventId.IsNone())
        {
            Response->SetField(TEXT("active_event"), MakeShared<FJsonValueNull>());
        }
        else
        {
            Response->SetStringField(TEXT("active_event"), Result.ActiveEventId.ToString());
        }
    }
    if (!Result.TargetSemanticId.IsEmpty())
    {
        Response->SetStringField(TEXT("target_semantic_id"), Result.TargetSemanticId);
    }
    if (!Result.State.SemanticId.IsEmpty())
    {
        Response->SetObjectField(TEXT("state"), RuntimeStateJson(Result.State));
    }
    return SerializeObject(Response);
}

FString RendererStatusResponse(const FVistaRendererStatusResult& Result)
{
    if (!Result.bSucceeded)
    {
        return ErrorResponse(Result.CommandId.ToString(), *Result.Code.ToString());
    }
    const TSharedRef<FJsonObject> Response = MakeShared<FJsonObject>();
    Response->SetStringField(TEXT("command_id"), Result.CommandId.ToString());
    Response->SetStringField(TEXT("status"), TEXT("success"));
    Response->SetStringField(TEXT("code"), Result.Code.ToString());
    Response->SetStringField(
        TEXT("schema_version"),
        TEXT("simworld.vista.playable-home-renderer-status/v1"));
    Response->SetStringField(
        TEXT("unreal_engine_version"),
        Result.Observation.UnrealEngineVersion);
    Response->SetStringField(TEXT("rhi"), Result.Observation.Rhi);
    Response->SetStringField(TEXT("feature_level"), Result.Observation.FeatureLevel);
    Response->SetStringField(TEXT("shader_platform"), Result.Observation.ShaderPlatform);
    const TSharedRef<FJsonObject> CVars = MakeShared<FJsonObject>();
    for (const TPair<FString, double>& Pair :
         Result.Observation.ConsoleVariables)
    {
        CVars->SetNumberField(Pair.Key, Pair.Value);
    }
    Response->SetObjectField(TEXT("cvars"), CVars);
    return SerializeObject(Response);
}

UVistaPlayableHomeRuntimeSubsystem* FindRuntimeSubsystem()
{
    if (!GEngine)
    {
        return nullptr;
    }
    for (const FWorldContext& Context : GEngine->GetWorldContexts())
    {
        UWorld* World = Context.World();
        if (IsValid(World) &&
            (World->WorldType == EWorldType::Game ||
             World->WorldType == EWorldType::PIE ||
             World->WorldType == EWorldType::GamePreview))
        {
            if (UVistaPlayableHomeRuntimeSubsystem* Runtime =
                    World->GetSubsystem<UVistaPlayableHomeRuntimeSubsystem>())
            {
                return Runtime;
            }
        }
    }
    return nullptr;
}

bool ReadEnvelope(const TSharedPtr<FJsonObject>& Params,
                  FVistaLiveCommandEnvelope& Envelope,
                  FString& CommandId)
{
    FString Revision;
    if (!ReadString(Params, TEXT("command_id"), CommandId) || !IsCommandId(CommandId) ||
        !ReadString(Params, TEXT("expected_revision"), Revision) || !IsRevision(Revision) ||
        !ReadGeneration(Params, Envelope.SessionGeneration))
    {
        return false;
    }
    Envelope.CommandId = FName(*CommandId);
    Envelope.ExpectedRevision = FName(*Revision);
    return true;
}

TOptional<EVistaAffordance> ParseAffordance(const FString& Value)
{
    if (Value == TEXT("open")) return EVistaAffordance::Open;
    if (Value == TEXT("close")) return EVistaAffordance::Close;
    if (Value == TEXT("pick_up")) return EVistaAffordance::PickUp;
    if (Value == TEXT("drop")) return EVistaAffordance::Drop;
    if (Value == TEXT("place")) return EVistaAffordance::Place;
    if (Value == TEXT("toggle")) return EVistaAffordance::Toggle;
    if (Value == TEXT("sit")) return EVistaAffordance::Sit;
    if (Value == TEXT("inspect")) return EVistaAffordance::Inspect;
    return {};
}

TOptional<EVistaNpcActionType> ParseNpcAction(const FString& Value)
{
    if (Value == TEXT("navigate_to")) return EVistaNpcActionType::NavigateTo;
    if (Value == TEXT("look_at")) return EVistaNpcActionType::LookAt;
    if (Value == TEXT("pick_up")) return EVistaNpcActionType::PickUp;
    if (Value == TEXT("place")) return EVistaNpcActionType::Place;
    if (Value == TEXT("open_door")) return EVistaNpcActionType::OpenDoor;
    if (Value == TEXT("close_door")) return EVistaNpcActionType::CloseDoor;
    if (Value == TEXT("sit")) return EVistaNpcActionType::Sit;
    if (Value == TEXT("wait")) return EVistaNpcActionType::Wait;
    if (Value == TEXT("speak")) return EVistaNpcActionType::Speak;
    return {};
}

FString DispatchTyped(const TSharedPtr<FJsonObject>& Params)
{
    FString Operation;
    FString CommandId;
    if (!ReadString(Params, TEXT("operation"), Operation))
    {
        return ErrorResponse(TEXT(""), TEXT("OPERATION_REQUIRED"));
    }

    UVistaPlayableHomeRuntimeSubsystem* Runtime = FindRuntimeSubsystem();
    if (!IsValid(Runtime))
    {
        ReadString(Params, TEXT("command_id"), CommandId);
        return ErrorResponse(CommandId, TEXT("RUNTIME_UNAVAILABLE"));
    }

    if (Operation == TEXT("status") || Operation == TEXT("health"))
    {
        if (!ExactKeys(Params,
                       KeySet({TEXT("operation"), TEXT("command_id")}),
                       TSet<FString>()) ||
            !ReadString(Params, TEXT("command_id"), CommandId) ||
            !IsCommandId(CommandId))
        {
            return ErrorResponse(CommandId, TEXT("STATUS_SHAPE_INVALID"));
        }
        return ResultResponse(Runtime->GetStatus(FName(*CommandId)), true);
    }

    if (Operation == TEXT("renderer_status"))
    {
        if (!ExactKeys(Params,
                       KeySet({TEXT("operation"), TEXT("command_id")}),
                       TSet<FString>()) ||
            !ReadString(Params, TEXT("command_id"), CommandId) ||
            !IsCommandId(CommandId))
        {
            return ErrorResponse(CommandId, TEXT("RENDERER_STATUS_SHAPE_INVALID"));
        }
        return RendererStatusResponse(
            Runtime->GetRendererStatus(FName(*CommandId)));
    }

    if (Operation == TEXT("interaction"))
    {
        const TSet<FString> Required = KeySet({
            TEXT("operation"), TEXT("command_id"), TEXT("expected_revision"),
            TEXT("session_generation"), TEXT("requester_semantic_id"),
            TEXT("target_semantic_id"), TEXT("affordance")});
        const TSet<FString> Optional = KeySet({TEXT("placement_anchor_semantic_id")});
        if (!ExactKeys(Params, Required, Optional))
        {
            return ErrorResponse(TEXT(""), TEXT("INTERACTION_SHAPE_INVALID"));
        }
        FVistaLiveInteractionCommand Command;
        FString Affordance;
        if (!ReadEnvelope(Params, Command.Envelope, CommandId) ||
            !ReadString(Params, TEXT("requester_semantic_id"), Command.RequesterSemanticId) ||
            !IsSemanticId(Command.RequesterSemanticId) ||
            !ReadString(Params, TEXT("target_semantic_id"), Command.TargetSemanticId) ||
            !IsSemanticId(Command.TargetSemanticId) ||
            !ReadString(Params, TEXT("affordance"), Affordance))
        {
            return ErrorResponse(CommandId, TEXT("INTERACTION_VALUE_INVALID"));
        }
        const TOptional<EVistaAffordance> Parsed = ParseAffordance(Affordance);
        if (!Parsed.IsSet())
        {
            return ErrorResponse(CommandId, TEXT("AFFORDANCE_UNSUPPORTED"));
        }
        Command.Affordance = Parsed.GetValue();
        if (Params->HasField(TEXT("placement_anchor_semantic_id")) &&
            (!ReadString(Params, TEXT("placement_anchor_semantic_id"),
                         Command.PlacementAnchorSemanticId) ||
             !IsSemanticId(Command.PlacementAnchorSemanticId)))
        {
            return ErrorResponse(CommandId, TEXT("PLACEMENT_ANCHOR_INVALID"));
        }
        return ResultResponse(Runtime->ExecuteInteraction(Command));
    }

    if (Operation == TEXT("npc_queue"))
    {
        const TSet<FString> Required = KeySet({
            TEXT("operation"), TEXT("command_id"), TEXT("expected_revision"),
            TEXT("session_generation"), TEXT("npc_semantic_id"), TEXT("replace"),
            TEXT("actions")});
        if (!ExactKeys(Params, Required, TSet<FString>()))
        {
            return ErrorResponse(TEXT(""), TEXT("NPC_QUEUE_SHAPE_INVALID"));
        }
        FVistaLiveNpcQueueCommand Command;
        if (!ReadEnvelope(Params, Command.Envelope, CommandId) ||
            !ReadString(Params, TEXT("npc_semantic_id"), Command.NpcSemanticId) ||
            !IsSemanticId(Command.NpcSemanticId) ||
            !Params->TryGetBoolField(TEXT("replace"), Command.bReplace) || !Command.bReplace)
        {
            return ErrorResponse(CommandId, TEXT("NPC_QUEUE_VALUE_INVALID"));
        }
        const TArray<TSharedPtr<FJsonValue>>* Actions = nullptr;
        if (!Params->TryGetArrayField(TEXT("actions"), Actions) ||
            Actions == nullptr || Actions->IsEmpty() || Actions->Num() > 32)
        {
            return ErrorResponse(CommandId, TEXT("NPC_ACTIONS_INVALID"));
        }
        TSet<FName> ActionIds;
        for (const TSharedPtr<FJsonValue>& Value : *Actions)
        {
            const TSharedPtr<FJsonObject>* ActionObject = nullptr;
            if (!Value.IsValid() || !Value->TryGetObject(ActionObject) || ActionObject == nullptr ||
                !ExactKeys(*ActionObject,
                    KeySet({TEXT("action_id"), TEXT("type")}),
                    KeySet({TEXT("target_semantic_id"), TEXT("target_location_cm"),
                            TEXT("duration_sec"), TEXT("timeout_sec"), TEXT("speech")})))
            {
                return ErrorResponse(CommandId, TEXT("NPC_ACTION_SHAPE_INVALID"));
            }
            FVistaNpcAction Action;
            FString ActionId;
            FString Type;
            if (!ReadString(*ActionObject, TEXT("action_id"), ActionId) ||
                !IsActionId(ActionId) ||
                !ReadString(*ActionObject, TEXT("type"), Type))
            {
                return ErrorResponse(CommandId, TEXT("NPC_ACTION_VALUE_INVALID"));
            }
            const TOptional<EVistaNpcActionType> Parsed = ParseNpcAction(Type);
            if (!Parsed.IsSet() || ActionIds.Contains(FName(*ActionId)))
            {
                return ErrorResponse(CommandId, TEXT("NPC_ACTION_UNSUPPORTED_OR_DUPLICATE"));
            }
            Action.ActionId = FName(*ActionId);
            Action.Type = Parsed.GetValue();
            ActionIds.Add(Action.ActionId);
            if ((*ActionObject)->HasField(TEXT("target_semantic_id")) &&
                (!ReadString(*ActionObject, TEXT("target_semantic_id"), Action.TargetSemanticId) ||
                 !IsSemanticId(Action.TargetSemanticId)))
            {
                return ErrorResponse(CommandId, TEXT("NPC_TARGET_INVALID"));
            }
            const TArray<TSharedPtr<FJsonValue>>* Location = nullptr;
            if ((*ActionObject)->HasField(TEXT("target_location_cm")))
            {
                if (!(*ActionObject)->TryGetArrayField(TEXT("target_location_cm"), Location) ||
                    Location == nullptr || Location->Num() != 3)
                {
                    return ErrorResponse(CommandId, TEXT("NPC_LOCATION_INVALID"));
                }
                double Components[3];
                for (int32 Index = 0; Index < 3; ++Index)
                {
                    if (!(*Location)[Index]->TryGetNumber(Components[Index]) ||
                        !FMath::IsFinite(Components[Index]) || FMath::Abs(Components[Index]) > 10000000.0)
                    {
                        return ErrorResponse(CommandId, TEXT("NPC_LOCATION_INVALID"));
                    }
                }
                Action.TargetLocation = FVector(Components[0], Components[1], Components[2]);
            }
            double Number = 0.0;
            if ((*ActionObject)->HasField(TEXT("duration_sec")))
            {
                if (!(*ActionObject)->TryGetNumberField(TEXT("duration_sec"), Number) ||
                    !FMath::IsFinite(Number) || Number < 0.0 || Number > 300.0)
                {
                    return ErrorResponse(CommandId, TEXT("NPC_DURATION_INVALID"));
                }
                Action.DurationSeconds = Number;
            }
            if ((*ActionObject)->HasField(TEXT("timeout_sec")))
            {
                if (!(*ActionObject)->TryGetNumberField(TEXT("timeout_sec"), Number) ||
                    !FMath::IsFinite(Number) || Number < 0.0 || Number > 300.0)
                {
                    return ErrorResponse(CommandId, TEXT("NPC_TIMEOUT_INVALID"));
                }
                Action.TimeoutSeconds = Number;
            }
            if ((*ActionObject)->HasField(TEXT("speech")) &&
                (!ReadString(*ActionObject, TEXT("speech"), Action.Speech) || Action.Speech.Len() > 500))
            {
                return ErrorResponse(CommandId, TEXT("NPC_SPEECH_INVALID"));
            }
            Command.Actions.Add(Action);
        }
        return ResultResponse(Runtime->ExecuteNpcQueue(Command));
    }

    if (Operation == TEXT("event"))
    {
        const TSet<FString> Required = KeySet({
            TEXT("operation"), TEXT("command_id"), TEXT("expected_revision"),
            TEXT("session_generation"), TEXT("event_operation")});
        const TSet<FString> Optional = KeySet({TEXT("event_id")});
        if (!ExactKeys(Params, Required, Optional))
        {
            return ErrorResponse(TEXT(""), TEXT("EVENT_SHAPE_INVALID"));
        }
        FVistaLiveEventCommand Command;
        FString EventOperation;
        if (!ReadEnvelope(Params, Command.Envelope, CommandId) ||
            !ReadString(Params, TEXT("event_operation"), EventOperation))
        {
            return ErrorResponse(CommandId, TEXT("EVENT_VALUE_INVALID"));
        }
        if (EventOperation == TEXT("start_event"))
        {
            FString EventId;
            if (!ReadString(Params, TEXT("event_id"), EventId) || !IsRevision(EventId))
            {
                return ErrorResponse(CommandId, TEXT("EVENT_ID_INVALID"));
            }
            Command.Operation = EVistaLiveEventOperation::StartEvent;
            Command.EventId = FName(*EventId);
        }
        else if (EventOperation == TEXT("reset_event") &&
                 !Params->HasField(TEXT("event_id")))
        {
            Command.Operation = EVistaLiveEventOperation::ResetEvent;
        }
        else
        {
            return ErrorResponse(CommandId, TEXT("EVENT_OPERATION_UNSUPPORTED"));
        }
        return ResultResponse(Runtime->ExecuteEvent(Command));
    }

    ReadString(Params, TEXT("command_id"), CommandId);
    return ErrorResponse(CommandId, TEXT("OPERATION_UNSUPPORTED"));
}
} // namespace

FVistaWorldTcpAdapter::FVistaWorldTcpAdapter(uint16 InPort) : Port(InPort) {}

FVistaWorldTcpAdapter::~FVistaWorldTcpAdapter()
{
    Stop();
}

bool FVistaWorldTcpAdapter::Start()
{
    if (Listener)
    {
        return false;
    }
    const FIPv4Endpoint Endpoint(FIPv4Address::InternalLoopback, Port);
    Listener = MakeUnique<FTcpListener>(Endpoint, FTimespan::FromMilliseconds(10));
    if (!Listener || !Listener->IsActive())
    {
        Listener.Reset();
        return false;
    }
    Listener->OnConnectionAccepted().BindRaw(this, &FVistaWorldTcpAdapter::HandleConnection);
    UE_LOG(LogTemp, Display, TEXT("VISTA World fixed adapter listening on 127.0.0.1:%u"), Port);
    return true;
}

void FVistaWorldTcpAdapter::Stop()
{
    if (Listener)
    {
        Listener->Stop();
        Listener.Reset();
    }
}

bool FVistaWorldTcpAdapter::HandleConnection(
    FSocket* Socket,
    const FIPv4Endpoint& RemoteEndpoint)
{
    if (!Socket)
    {
        return false;
    }
    FString Response;
    if (RemoteEndpoint.Address != FIPv4Address::InternalLoopback)
    {
        Response = ErrorResponse(TEXT(""), TEXT("LOOPBACK_REQUIRED"));
    }
    else
    {
        Socket->SetNonBlocking(false);
        int32 ActualBufferSize = 0;
        Socket->SetReceiveBufferSize(MaxRequestBytes, ActualBufferSize);
        TArray<uint8> Buffer;
        Buffer.Reserve(MaxRequestBytes);
        const double Deadline = FPlatformTime::Seconds() + ReadTimeoutSeconds;
        bool bComplete = false;
        while (FPlatformTime::Seconds() < Deadline && Buffer.Num() < MaxRequestBytes)
        {
            if (!Socket->Wait(ESocketWaitConditions::WaitForRead,
                              FTimespan::FromMilliseconds(100)))
            {
                continue;
            }
            uint8 Chunk[4096];
            int32 Read = 0;
            if (!Socket->Recv(Chunk, UE_ARRAY_COUNT(Chunk), Read) || Read <= 0)
            {
                break;
            }
            for (int32 Index = 0; Index < Read; ++Index)
            {
                if (Chunk[Index] == '\n')
                {
                    bComplete = true;
                    break;
                }
                Buffer.Add(Chunk[Index]);
                if (Buffer.Num() >= MaxRequestBytes)
                {
                    break;
                }
            }
            if (bComplete)
            {
                break;
            }
        }
        if (!bComplete || Buffer.IsEmpty())
        {
            Response = ErrorResponse(TEXT(""),
                Buffer.Num() >= MaxRequestBytes ? TEXT("REQUEST_TOO_LARGE")
                                                : TEXT("REQUEST_FRAME_INCOMPLETE"));
        }
        else
        {
            Buffer.Add(0);
            const FString Frame = UTF8_TO_TCHAR(reinterpret_cast<const char*>(Buffer.GetData()));
            Response = DispatchFrame(Frame);
        }
    }

    FTCHARToUTF8 Encoded(*Response);
    if (Encoded.Length() > MaxResponseBytes)
    {
        Response = ErrorResponse(TEXT(""), TEXT("RESPONSE_TOO_LARGE"));
    }
    FTCHARToUTF8 FinalBytes(*Response);
    int32 Offset = 0;
    while (Offset < FinalBytes.Length())
    {
        int32 Sent = 0;
        if (!Socket->Send(
                reinterpret_cast<const uint8*>(FinalBytes.Get()) + Offset,
                FinalBytes.Length() - Offset,
                Sent) || Sent <= 0)
        {
            break;
        }
        Offset += Sent;
    }
    Socket->Close();
    ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->DestroySocket(Socket);
    return true;
}

FString FVistaWorldTcpAdapter::DispatchFrame(const FString& Frame)
{
    TSharedPtr<FJsonObject> Root;
    const TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(Frame);
    if (!FJsonSerializer::Deserialize(Reader, Root) ||
        !ExactKeys(Root, KeySet({TEXT("type"), TEXT("params")}), TSet<FString>()))
    {
        return ErrorResponse(TEXT(""), TEXT("REQUEST_JSON_INVALID"));
    }
    FString Type;
    const TSharedPtr<FJsonObject>* Params = nullptr;
    if (!ReadString(Root, TEXT("type"), Type) || Type != TEXT("vista_world_action") ||
        !Root->TryGetObjectField(TEXT("params"), Params) || Params == nullptr)
    {
        return ErrorResponse(TEXT(""), TEXT("TOOL_UNSUPPORTED"));
    }

    const TSharedRef<FDispatchState, ESPMode::ThreadSafe> State =
        MakeShared<FDispatchState, ESPMode::ThreadSafe>();
    const TSharedPtr<FJsonObject> TypedParams = *Params;
    AsyncTask(ENamedThreads::GameThread, [State, TypedParams]()
    {
        {
            FScopeLock Lock(&State->Mutex);
            if (State->bCanceled)
            {
                State->Completed->Trigger();
                return;
            }
        }
        const FString Response = DispatchTyped(TypedParams);
        {
            FScopeLock Lock(&State->Mutex);
            if (!State->bCanceled)
            {
                State->Response = Response;
            }
        }
        State->Completed->Trigger();
    });
    if (!State->Completed->Wait(DispatchTimeoutMilliseconds))
    {
        FScopeLock Lock(&State->Mutex);
        State->bCanceled = true;
        FString CommandId;
        ReadString(TypedParams, TEXT("command_id"), CommandId);
        return ErrorResponse(CommandId, TEXT("DISPATCH_TIMEOUT"));
    }
    FScopeLock Lock(&State->Mutex);
    return State->Response.IsEmpty()
        ? ErrorResponse(TEXT(""), TEXT("DISPATCH_FAILED"))
        : State->Response;
}
