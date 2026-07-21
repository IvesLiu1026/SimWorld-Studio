#include "VistaAnimationContentApiSubsystem.h"

#include "VistaAnimationStrictJson.h"

#include "HAL/Platform.h"
#include "HAL/PlatformTime.h"
#include "Misc/Guid.h"
#include "Misc/Paths.h"
#include "Misc/ScopeLock.h"
#include "Modules/ModuleManager.h"
#include "Runtime/Launch/Resources/Version.h"

using namespace VistaAnimation::StrictJson;

namespace {
constexpr int32 MaxCapabilityRequestBytes = 131072;
constexpr int32 MaxContentRequestBytes = 1048576;
constexpr int32 MaxEngineTimeRequestBytes = 131072;
constexpr int32 MaxEvidenceCaptureRequestBytes = 1048576;
constexpr int32 MaxReplayEntries = 4096;
constexpr int32 MaxJournalEntries = 2048;
constexpr int32 MaxLifecycleEntries = 4096;
constexpr double MaxEngineTimeSec = 315360000.0;

const TCHAR *CapabilitySchema = TEXT("vista-animation-ue-capability-probe/v1");
const TCHAR *CapabilityResponseSchema =
    TEXT("vista-animation-ue-capability/v1");
const TCHAR *CapabilityOperationId = TEXT("vista.animation.capabilities.v1");
const TCHAR *CapabilityFingerprint =
    TEXT("ab2e3e17bbf8a612054c09270ed7e593dab1706baa682e9e7da732c58a46692b");
const TCHAR *RequestEnvelopeSchema = TEXT("vista-animation-ue-request/v1");
const TCHAR *ResponseEnvelopeSchema = TEXT("vista-animation-ue-response/v1");
const TCHAR *NonceSchema = TEXT("vista-animation-ue-nonce-marker/v1");
const TCHAR *SlotSchema = TEXT("vista-animation-ue-slot-binding/v1");
const TCHAR *ContentProofSchema = TEXT("vista-animation-ue-content-proof/v1");
const TCHAR *EngineTimeRequestSchema =
    TEXT("vista-animation-engine-time-request/v1");
const TCHAR *EngineTimeResponseSchema =
    TEXT("vista-animation-engine-time-response/v1");
const TCHAR *EvidenceCaptureRequestSchema =
    TEXT("vista-animation-evidence-capture-request/v1");
const TCHAR *EvidenceCaptureResponseSchema =
    TEXT("vista-animation-evidence-capture-response/v1");
const TCHAR *EvidenceContextSchema =
    TEXT("vista-animation-evidence-hook-context/v1");
const TCHAR *OperationAllowlistDigest =
    TEXT("851302cc75bd70e99536fa4e7adbf359ab95d94f104a369108bda91b8377c1a3");

struct FOperationContract {
  const TCHAR *OperationId;
  const TCHAR *Fingerprint;
  const TCHAR *RequestSchema;
  const TCHAR *ResponseSchema;
  bool bMutation;
  int32 MaxAttempts;
};

const FOperationContract Operations[] = {
    {TEXT("vista.animation.preflight.v1"),
     TEXT("44491226546ca0d78bc5d968a2d3ea98474a07a307256520e2321025957e56d0"),
     TEXT("vista-animation-preflight-request/v1"),
     TEXT("vista-animation-preflight-response/v1"), false, 2},
    {TEXT("vista.animation.release.v1"),
     TEXT("958c71ab9525719a6e570c148563fef78bde3915a3ba3ac752c843a62f4f5674"),
     TEXT("vista-animation-release-request/v1"),
     TEXT("vista-animation-release-response/v1"), true, 1},
    {TEXT("vista.animation.restore.v1"),
     TEXT("f47d059b6aa522571c68b1e7e726d1d5d5625fac330d3cc8413be1a1cce498a7"),
     TEXT("vista-animation-restore-request/v1"),
     TEXT("vista-animation-restore-response/v1"), true, 1},
    {TEXT("vista.animation.snapshot.v1"),
     TEXT("2f00085b3d8474fc77bd395cea9fa4d8965320dc1a7413963ef4be49e63ff95d"),
     TEXT("vista-animation-snapshot-request/v1"),
     TEXT("vista-animation-snapshot-response/v1"), false, 2},
    {TEXT("vista.animation.start.v1"),
     TEXT("9786b50f455a9277ddc857ec1aa4165a4110cc6d76a76f7e5ff7b7706ee4c30e"),
     TEXT("vista-animation-start-request/v1"),
     TEXT("vista-animation-start-response/v1"), true, 1},
    {TEXT("vista.animation.stop.v1"),
     TEXT("39decfb45b7b691d3d962e63c4ad1d1e1de5c1e2c2ced610c7a158116ec16c38"),
     TEXT("vista-animation-stop-request/v1"),
     TEXT("vista-animation-stop-response/v1"), true, 1},
    {TEXT("vista.animation.wait.v1"),
     TEXT("151df1d501b0ad2ba96604243cfa0a23a1aa7dffca8c6435e0b4a330e0596916"),
     TEXT("vista-animation-wait-request/v1"),
     TEXT("vista-animation-wait-response/v1"), false, 2}};

struct FActionContract {
  EVistaAnimationAction Action;
  const TCHAR *Name;
  const TCHAR *BridgeActionId;
  enum class ETargetPolicy : uint8 {
    Required,
    Optional,
    Forbidden
  } TargetPolicy;
};

const FActionContract Actions[] = {
    {EVistaAnimationAction::LookAt, TEXT("look_at"), TEXT("vista_look_at_v1"),
     FActionContract::ETargetPolicy::Required},
    {EVistaAnimationAction::PickUp, TEXT("pick_up"),
     TEXT("vista_pick_up_ik_v1"), FActionContract::ETargetPolicy::Required},
    {EVistaAnimationAction::Brace, TEXT("brace"), TEXT("vista_brace_ik_v1"),
     FActionContract::ETargetPolicy::Required},
    {EVistaAnimationAction::Drag, TEXT("drag"), TEXT("vista_drag_ik_v1"),
     FActionContract::ETargetPolicy::Required},
    {EVistaAnimationAction::LiftFoot, TEXT("lift_foot"),
     TEXT("vista_lift_foot_ik_v1"), FActionContract::ETargetPolicy::Required},
    {EVistaAnimationAction::Pause, TEXT("pause"), TEXT("vista_pause_pose_v1"),
     FActionContract::ETargetPolicy::Optional},
    {EVistaAnimationAction::Fall, TEXT("fall"), TEXT("vista_fall_montage_v1"),
     FActionContract::ETargetPolicy::Forbidden},
    {EVistaAnimationAction::Recover, TEXT("recover"),
     TEXT("vista_recover_montage_v1"),
     FActionContract::ETargetPolicy::Forbidden}};

const FOperationContract *FindOperation(const FString &Id) {
  for (const FOperationContract &Operation : Operations)
    if (Id == Operation.OperationId)
      return &Operation;
  return nullptr;
}

const FActionContract *FindActionByName(const FString &Name) {
  for (const FActionContract &Action : Actions)
    if (Name == Action.Name)
      return &Action;
  return nullptr;
}

const FActionContract *FindActionByBridgeId(const FString &Id) {
  for (const FActionContract &Action : Actions)
    if (Id == Action.BridgeActionId)
      return &Action;
  return nullptr;
}

const FActionContract *FindAction(EVistaAnimationAction Value) {
  for (const FActionContract &Action : Actions)
    if (Value == Action.Action)
      return &Action;
  return nullptr;
}

bool IsAsciiAlphaNumeric(TCHAR Character) {
  return (Character >= TEXT('a') && Character <= TEXT('z')) ||
         (Character >= TEXT('A') && Character <= TEXT('Z')) ||
         (Character >= TEXT('0') && Character <= TEXT('9'));
}

bool IsOpaqueId(const FString &Value) {
  if (Value.IsEmpty() || Value.Len() > 160 || !IsAsciiAlphaNumeric(Value[0]))
    return false;
  for (const TCHAR Character : Value) {
    if (!IsAsciiAlphaNumeric(Character) && Character != TEXT('.') &&
        Character != TEXT('_') && Character != TEXT(':') &&
        Character != TEXT('@') && Character != TEXT('-'))
      return false;
  }
  return true;
}

bool IsSafeId(const FString &Value) {
  if (Value.IsEmpty() || Value.Len() > 120 || Value[0] < TEXT('a') ||
      Value[0] > TEXT('z'))
    return false;
  for (const TCHAR Character : Value) {
    if ((Character < TEXT('a') || Character > TEXT('z')) &&
        (Character < TEXT('0') || Character > TEXT('9')) &&
        Character != TEXT('_') && Character != TEXT('-'))
      return false;
  }
  return true;
}

bool IsLowerHex(const FString &Value, int32 Length) {
  if (Value.Len() != Length)
    return false;
  for (const TCHAR Character : Value) {
    if ((Character < TEXT('0') || Character > TEXT('9')) &&
        (Character < TEXT('a') || Character > TEXT('f')))
      return false;
  }
  return true;
}

bool IsPreflightId(const FString &Value) {
  return Value.StartsWith(TEXT("vap-")) && IsLowerHex(Value.RightChop(4), 24);
}

bool IsTimelineId(const FString &Value) {
  return Value.StartsWith(TEXT("vtl-")) &&
         IsLowerHex(Value.RightChop(4), 24);
}

bool IsEventId(const FString &Value) {
  if (!Value.StartsWith(TEXT("beat-")) || Value.Len() < 9)
    return false;
  for (int32 Index = 5; Index < 9; ++Index)
    if (!FChar::IsDigit(Value[Index]))
      return false;
  if (Value.Len() == 9)
    return true;
  return Value[9] == TEXT('-') &&
         IsSafeId(FString(TEXT("a")) + Value.RightChop(10));
}

bool IsSafeErrorCode(const FString &Value) {
  if (Value.IsEmpty() || Value.Len() > 80)
    return false;
  for (const TCHAR Character : Value) {
    if ((Character < TEXT('A') || Character > TEXT('Z')) &&
        (Character < TEXT('0') || Character > TEXT('9')) &&
        Character != TEXT('_'))
      return false;
  }
  return true;
}

bool IsSafeArtifactRef(const FString &Value) {
  if (Value.IsEmpty() || Value.Len() > 512 ||
      !IsAsciiAlphaNumeric(Value[0]) || Value.Contains(TEXT("..")) ||
      Value.Contains(TEXT("//")))
    return false;
  for (const TCHAR Character : Value) {
    if (!IsAsciiAlphaNumeric(Character) && Character != TEXT('.') &&
        Character != TEXT('_') && Character != TEXT('/') &&
        Character != TEXT('@') && Character != TEXT('-'))
      return false;
  }
  return true;
}

bool ParseEvidenceKind(const FString &Name,
                       EVistaAnimationEvidenceKind &OutKind) {
  if (Name == TEXT("pose_snapshot"))
    OutKind = EVistaAnimationEvidenceKind::PoseSnapshot;
  else if (Name == TEXT("interaction_state"))
    OutKind = EVistaAnimationEvidenceKind::InteractionState;
  else if (Name == TEXT("screenshot"))
    OutKind = EVistaAnimationEvidenceKind::Screenshot;
  else if (Name == TEXT("scene_validation"))
    OutKind = EVistaAnimationEvidenceKind::SceneValidation;
  else
    return false;
  return true;
}

bool ParseEvidencePhase(const FString &Name,
                        EVistaAnimationEvidencePhase &OutPhase) {
  if (Name == TEXT("before"))
    OutPhase = EVistaAnimationEvidencePhase::Before;
  else if (Name == TEXT("after"))
    OutPhase = EVistaAnimationEvidencePhase::After;
  else if (Name == TEXT("rollback"))
    OutPhase = EVistaAnimationEvidencePhase::Rollback;
  else if (Name == TEXT("terminal"))
    OutPhase = EVistaAnimationEvidencePhase::Terminal;
  else
    return false;
  return true;
}

bool ReadRequiredString(const FValue &Object, const TCHAR *Name, FString &Out) {
  const FValue *Value = Field(Object, Name);
  return Value && ReadString(*Value, Out);
}

bool ReadRequiredNumber(const FValue &Object, const TCHAR *Name, double &Out) {
  const FValue *Value = Field(Object, Name);
  return Value && ReadNumber(*Value, Out);
}

bool ReadRequiredBoolean(const FValue &Object, const TCHAR *Name, bool &Out) {
  const FValue *Value = Field(Object, Name);
  return Value && ReadBoolean(*Value, Out);
}

bool ReadRequiredNullableString(const FValue &Object, const TCHAR *Name,
                                TOptional<FString> &Out) {
  const FValue *Value = Field(Object, Name);
  return Value && ReadNullableString(*Value, Out);
}

bool ReadSafeStringArray(const FValue &Object, const TCHAR *Name,
                         TArray<FString> &Out, bool bAllowEmpty) {
  const FValue *Value = Field(Object, Name);
  if (!Value || Value->Kind != EKind::Array || Value->Array.Num() > 128 ||
      (!bAllowEmpty && Value->Array.IsEmpty()))
    return false;
  Out.Reset();
  FString Previous;
  for (const TSharedPtr<FValue> &Entry : Value->Array) {
    FString Item;
    if (!ReadString(*Entry, Item) || !IsSafeId(Item) ||
        (!Previous.IsEmpty() && Item <= Previous))
      return false;
    Out.Add(Item);
    Previous = MoveTemp(Item);
  }
  return true;
}

FString JsonStringArray(const TArray<FString> &Values) {
  TArray<FString> Encoded;
  Encoded.Reserve(Values.Num());
  for (const FString &Value : Values)
    Encoded.Add(Quote(Value));
  return FString::Printf(TEXT("[%s]"), *FString::Join(Encoded, TEXT(",")));
}

FString BuildAnimationErrorJson(const FString &CandidateCode,
                                bool bRetryable = false) {
  const FString Code = IsSafeErrorCode(CandidateCode)
                           ? CandidateCode
                           : TEXT("ANIMATION_CONTENT_API_REJECTED");
  return FString::Printf(TEXT("{\"schema\":\"vista-animation-ue-error/"
                              "v1\",\"code\":%s,\"retryable\":%s}"),
                         *Quote(Code),
                         bRetryable ? TEXT("true") : TEXT("false"));
}

// Unreal 5.7 exports a generic MakeError forwarding template. Keep the
// protocol helper's existing call sites explicit at preprocessing time so a
// TCHAR literal cannot bind to that unrelated template.
#define MakeError(...) BuildAnimationErrorJson(__VA_ARGS__)

int32 Utf8Bytes(const FString &Value) {
  FTCHARToUTF8 Utf8(*Value);
  return Utf8.Length();
}

FString PlatformIdentity() {
#if PLATFORM_LINUX && PLATFORM_CPU_ARM_FAMILY
  return TEXT("linux-aarch64");
#elif PLATFORM_LINUX
  return TEXT("linux-x86_64");
#elif PLATFORM_WINDOWS && PLATFORM_64BITS
  return TEXT("windows-x86_64");
#elif PLATFORM_MAC && PLATFORM_CPU_ARM_FAMILY
  return TEXT("macos-aarch64");
#elif PLATFORM_MAC
  return TEXT("macos-x86_64");
#else
  return TEXT("unsupported-platform");
#endif
}

FString EngineIdentity() {
  return FString::Printf(TEXT("%d.%d.%d"), ENGINE_MAJOR_VERSION,
                         ENGINE_MINOR_VERSION, ENGINE_PATCH_VERSION);
}

FString EventKey(const FString &PreflightId, const FString &RunId,
                 const FString &EventId) {
  return PreflightId + TEXT("|") + RunId + TEXT("|") + EventId;
}
} // namespace

class UVistaAnimationContentApiSubsystem::FImplementation final {
public:
  bool Configure(
      const FVistaAnimationTrustedRuntimeConfig &InConfig,
      TSharedRef<IVistaAnimationContentDriver, ESPMode::ThreadSafe> InDriver,
      FString &OutSafeErrorCode) {
    FScopeLock Guard(&Mutex);
    if (bConfigured) {
      OutSafeErrorCode = TEXT("ANIMATION_ALREADY_CONFIGURED");
      return false;
    }
    if (!ValidateConfig(InConfig, OutSafeErrorCode))
      return false;
    FVistaAnimationDriverProfileProof DriverProof;
    DriverProof.ProfileId = InConfig.ContentProof.ProfileId;
    DriverProof.ProfileRevision = InConfig.ContentProof.ProfileRevision;
    DriverProof.ContentRevision = InConfig.ContentProof.ContentRevision;
    DriverProof.ContentDigest = InConfig.ContentProof.ContentDigest;
    DriverProof.VerificationReceiptId =
        InConfig.ContentProof.VerificationReceiptId;
    TArray<FVistaAnimationDriverTrustedAction> DriverActions;
    DriverActions.Reserve(InConfig.Actions.Num());
    for (const FVistaAnimationTrustedAction &Action : InConfig.Actions)
      DriverActions.Add({Action.Action, Action.AdapterId, Action.BridgeActionId,
                         Action.CompletionSignal, Action.TimeoutMs});
    FString DriverProfileError;
    if (!InDriver->ValidateTrustedProfile(DriverProof, DriverActions,
                                          DriverProfileError)) {
      OutSafeErrorCode = IsSafeErrorCode(DriverProfileError)
                             ? DriverProfileError
                             : TEXT("ANIMATION_DRIVER_PROFILE_UNVERIFIED");
      return false;
    }
    Config = InConfig;
    Driver = InDriver;
    ProcessInstanceId =
        TEXT("vapi-") +
        FGuid::NewGuid().ToString(EGuidFormats::Digits).ToLower();
    SlotBindingJson = BuildSlotBindingJson(Config.SlotBinding);
    RuntimeSlotBindingJson = BuildRuntimeSlotBindingJson(Config.SlotBinding);
    ContentProofJson = BuildContentProofJson(Config.ContentProof);
    bConfigured = true;
    return true;
  }

  void Reset() {
    FScopeLock Guard(&Mutex);
    bConfigured = false;
    Driver.Reset();
    UsedNonces.Reset();
    NonceOrder.Reset();
    Journal.Reset();
    JournalOrder.Reset();
    Snapshots.Reset();
    Handles.Reset();
    ActivePreflightId.Reset();
    LastPreflight = FPreflightState{};
    bPreflightInProgress = false;
    SlotBindingJson.Reset();
    RuntimeSlotBindingJson.Reset();
    ContentProofJson.Reset();
    ProcessInstanceId.Reset();
  }

  bool Capability(const FString &RequestJson, FString &OutResponseJson) {
    if (Utf8Bytes(RequestJson) > MaxCapabilityRequestBytes) {
      OutResponseJson =
          MakeError(TEXT("ANIMATION_CAPABILITY_REQUEST_TOO_LARGE"));
      return false;
    }
    TSharedPtr<FValue> Root;
    FString Error;
    if (!Parse(RequestJson, Root, Error) ||
        !ExactKeys(*Root,
                   {TEXT("schema"), TEXT("operation_id"),
                    TEXT("operation_fingerprint"), TEXT("nonce_marker"),
                    TEXT("slot_binding"), TEXT("content_proof"),
                    TEXT("operation_allowlist_digest"),
                    TEXT("challenge_digest")},
                   Error)) {
      OutResponseJson =
          MakeError(TEXT("ANIMATION_CAPABILITY_PROTOCOL_INVALID"));
      return false;
    }

    FString Schema, OperationId, Fingerprint, AllowlistDigest, ChallengeDigest;
    if (!ReadRequiredString(*Root, TEXT("schema"), Schema) ||
        !ReadRequiredString(*Root, TEXT("operation_id"), OperationId) ||
        !ReadRequiredString(*Root, TEXT("operation_fingerprint"),
                            Fingerprint) ||
        !ReadRequiredString(*Root, TEXT("operation_allowlist_digest"),
                            AllowlistDigest) ||
        !ReadRequiredString(*Root, TEXT("challenge_digest"), ChallengeDigest) ||
        Schema != CapabilitySchema || OperationId != CapabilityOperationId ||
        Fingerprint != CapabilityFingerprint ||
        AllowlistDigest != OperationAllowlistDigest ||
        !IsLowerHex(ChallengeDigest, 64)) {
      OutResponseJson =
          MakeError(TEXT("ANIMATION_CAPABILITY_IDENTITY_INVALID"));
      return false;
    }
    const FValue *NonceMarker = Field(*Root, TEXT("nonce_marker"));
    const FValue *SlotBinding = Field(*Root, TEXT("slot_binding"));
    const FValue *ContentProof = Field(*Root, TEXT("content_proof"));
    FString Nonce;
    if (!NonceMarker || !SlotBinding || !ContentProof ||
        !ValidateNonceMarker(*NonceMarker, Nonce) ||
        !ValidateSlotBinding(*SlotBinding) ||
        !ValidateContentProof(*ContentProof)) {
      OutResponseJson = MakeError(TEXT("ANIMATION_CAPABILITY_BINDING_INVALID"));
      return false;
    }

    const FString ChallengeCanonical = FString::Printf(
        TEXT("{\"content_proof\":%s,\"nonce_marker\":%s,\"operation_allowlist_"
             "digest\":%s,\"operation_fingerprint\":%s,\"operation_id\":%s,"
             "\"schema\":%s,\"slot_binding\":%s}"),
        *Canonicalize(*ContentProof), *Canonicalize(*NonceMarker),
        *Quote(AllowlistDigest), *Quote(Fingerprint), *Quote(OperationId),
        *Quote(Schema), *Canonicalize(*SlotBinding));
    if (Sha256HexUtf8(ChallengeCanonical) != ChallengeDigest) {
      OutResponseJson = MakeError(TEXT("ANIMATION_CAPABILITY_DIGEST_INVALID"));
      return false;
    }

    FString LocalSlot;
    FString LocalContent;
    FString LocalProcess;
    {
      FScopeLock Guard(&Mutex);
      if (!bConfigured) {
        OutResponseJson = MakeError(TEXT("ANIMATION_CONTENT_API_UNCONFIGURED"));
        return false;
      }
      if (SlotBindingJson != Canonicalize(*SlotBinding) ||
          ContentProofJson != Canonicalize(*ContentProof)) {
        OutResponseJson =
            MakeError(TEXT("ANIMATION_CAPABILITY_BINDING_MISMATCH"));
        return false;
      }
      if (!ConsumeNonceLocked(Nonce)) {
        OutResponseJson = MakeError(TEXT("ANIMATION_NONCE_REPLAYED"));
        return false;
      }
      LocalSlot = SlotBindingJson;
      LocalContent = ContentProofJson;
      LocalProcess = ProcessInstanceId;
    }

    FModuleStatus ModuleStatus;
    const bool bModuleKnown = FModuleManager::Get().QueryModule(
        FName(TEXT("VistaAnimationContentApi")), ModuleStatus);
    const FString ModuleFilename =
        bModuleKnown ? ModuleStatus.FilePath : FString();
    FString BinarySha;
    if (!bModuleKnown || !ModuleStatus.bIsLoaded || ModuleFilename.IsEmpty() ||
        !FPaths::GetCleanFilename(ModuleFilename)
             .Contains(TEXT("VistaAnimationContentApi"),
                       ESearchCase::CaseSensitive) ||
        !Sha256File(ModuleFilename, BinarySha)) {
      OutResponseJson = MakeError(TEXT("ANIMATION_PLUGIN_BINARY_UNVERIFIED"));
      return false;
    }

    OutResponseJson =
        BuildCapabilityResponse(Nonce, ChallengeDigest, LocalSlot, LocalContent,
                                LocalProcess, BinarySha);
    return true;
  }

  bool Content(const FString &RequestJson, FString &OutResponseJson) {
    if (Utf8Bytes(RequestJson) > MaxContentRequestBytes) {
      OutResponseJson = MakeError(TEXT("ANIMATION_CONTENT_REQUEST_TOO_LARGE"));
      return false;
    }
    TSharedPtr<FValue> Root;
    FString Error;
    if (!Parse(RequestJson, Root, Error) ||
        !ExactKeys(*Root,
                   {TEXT("schema"), TEXT("operation_id"),
                    TEXT("operation_fingerprint"), TEXT("invocation_id"),
                    TEXT("request_digest"), TEXT("content_proof"),
                    TEXT("nonce_marker"), TEXT("request")},
                   Error)) {
      OutResponseJson = MakeError(TEXT("ANIMATION_CONTENT_PROTOCOL_INVALID"));
      return false;
    }
    FEnvelope Envelope;
    if (!ValidateEnvelope(*Root, Envelope, OutResponseJson))
      return false;

    if (Envelope.Operation->bMutation) {
      FScopeLock Guard(&Mutex);
      const FJournalEntry *Existing = Journal.Find(Envelope.InvocationId);
      if (Existing) {
        if (Existing->RequestDigest == Envelope.RequestDigest &&
            Existing->Nonce == Envelope.Nonce &&
            !Existing->Response.IsEmpty()) {
          OutResponseJson = Existing->Response;
          return true;
        }
        OutResponseJson = MakeError(TEXT("ANIMATION_MUTATION_OUTCOME_UNKNOWN"));
        return false;
      }
    }

    {
      FScopeLock Guard(&Mutex);
      if (!bConfigured) {
        OutResponseJson = MakeError(TEXT("ANIMATION_CONTENT_API_UNCONFIGURED"));
        return false;
      }
      if (ContentProofJson != Envelope.ContentProofCanonical) {
        OutResponseJson = MakeError(TEXT("ANIMATION_CONTENT_PROOF_MISMATCH"));
        return false;
      }
      if (!ConsumeNonceLocked(Envelope.Nonce)) {
        OutResponseJson = MakeError(TEXT("ANIMATION_NONCE_REPLAYED"));
        return false;
      }
    }

    FString Payload;
    bool bSuccess = false;
    if (Envelope.Operation->OperationId ==
        FString(TEXT("vista.animation.preflight.v1")))
      bSuccess = HandlePreflight(Envelope, Payload);
    else if (Envelope.Operation->OperationId ==
             FString(TEXT("vista.animation.snapshot.v1")))
      bSuccess = HandleSnapshot(Envelope, Payload);
    else if (Envelope.Operation->OperationId ==
             FString(TEXT("vista.animation.start.v1")))
      bSuccess = HandleStart(Envelope, Payload);
    else if (Envelope.Operation->OperationId ==
             FString(TEXT("vista.animation.wait.v1")))
      bSuccess = HandleWait(Envelope, Payload);
    else if (Envelope.Operation->OperationId ==
             FString(TEXT("vista.animation.stop.v1")))
      bSuccess = HandleStop(Envelope, Payload);
    else if (Envelope.Operation->OperationId ==
             FString(TEXT("vista.animation.release.v1")))
      bSuccess = HandleRelease(Envelope, Payload);
    else if (Envelope.Operation->OperationId ==
             FString(TEXT("vista.animation.restore.v1")))
      bSuccess = HandleRestore(Envelope, Payload);

    if (!bSuccess) {
      OutResponseJson = Payload.IsEmpty()
                            ? MakeError(TEXT("ANIMATION_CONTENT_API_REJECTED"))
                            : Payload;
      return false;
    }
    OutResponseJson = BuildEnvelopeResponse(Envelope, Payload);
    if (Envelope.Operation->bMutation)
      CompleteMutation(Envelope, OutResponseJson);
    return true;
  }

  bool EngineTime(const FString &RequestJson, FString &OutResponseJson) {
    if (Utf8Bytes(RequestJson) > MaxEngineTimeRequestBytes) {
      OutResponseJson = MakeError(TEXT("ANIMATION_ENGINE_TIME_REQUEST_TOO_LARGE"));
      return false;
    }
    TSharedPtr<FValue> Root;
    FString Error, Schema, RunId, TimelineId, EventId, RequestDigest;
    if (!Parse(RequestJson, Root, Error) ||
        !ExactKeys(*Root,
                   {TEXT("schema"), TEXT("run_id"), TEXT("timeline_id"),
                    TEXT("event_id"), TEXT("slot_binding"),
                    TEXT("request_digest")},
                   Error) ||
        !ReadRequiredString(*Root, TEXT("schema"), Schema) ||
        Schema != EngineTimeRequestSchema ||
        !ReadRequiredString(*Root, TEXT("run_id"), RunId) ||
        !IsOpaqueId(RunId) ||
        !ReadRequiredString(*Root, TEXT("timeline_id"), TimelineId) ||
        !IsTimelineId(TimelineId) ||
        !ReadRequiredString(*Root, TEXT("event_id"), EventId) ||
        !IsEventId(EventId) ||
        !ReadRequiredString(*Root, TEXT("request_digest"), RequestDigest) ||
        !IsLowerHex(RequestDigest, 64)) {
      OutResponseJson = MakeError(TEXT("ANIMATION_ENGINE_TIME_PROTOCOL_INVALID"));
      return false;
    }
    const FValue *SlotBinding = Field(*Root, TEXT("slot_binding"));
    FString SlotCanonical;
    if (!SlotBinding ||
        !ValidateRuntimeSlotBinding(*SlotBinding, SlotCanonical)) {
      OutResponseJson = MakeError(TEXT("ANIMATION_ENGINE_TIME_BINDING_INVALID"));
      return false;
    }
    const FString DigestInput = FString::Printf(
        TEXT("{\"event_id\":%s,\"run_id\":%s,\"schema\":%s,"
             "\"slot_binding\":%s,\"timeline_id\":%s}"),
        *Quote(EventId), *Quote(RunId), *Quote(Schema), *SlotCanonical,
        *Quote(TimelineId));
    if (Sha256HexUtf8(DigestInput) != RequestDigest) {
      OutResponseJson = MakeError(TEXT("ANIMATION_ENGINE_TIME_DIGEST_INVALID"));
      return false;
    }

    double EngineTimeSec = 0.0;
    {
      FScopeLock Guard(&Mutex);
      if (!bConfigured) {
        OutResponseJson = MakeError(TEXT("ANIMATION_CONTENT_API_UNCONFIGURED"));
        return false;
      }
      if (RuntimeSlotBindingJson != SlotCanonical) {
        OutResponseJson =
            MakeError(TEXT("ANIMATION_ENGINE_TIME_BINDING_MISMATCH"));
        return false;
      }
      const double Elapsed =
          FPlatformTime::Seconds() - ProcessMonotonicOriginSec;
      if (!FMath::IsFinite(Elapsed) || Elapsed < 0.0 ||
          Elapsed > MaxEngineTimeSec) {
        OutResponseJson = MakeError(TEXT("ANIMATION_ENGINE_TIME_UNAVAILABLE"),
                                    true);
        return false;
      }
      EngineTimeSec = FMath::Max(Elapsed, LastEngineTimeSec);
      LastEngineTimeSec = EngineTimeSec;
    }

    OutResponseJson = FString::Printf(
        TEXT("{\"schema\":%s,\"run_id\":%s,\"timeline_id\":%s,"
             "\"event_id\":%s,\"slot_binding\":%s,\"request_digest\":%s,"
             "\"engine_time_sec\":%.17g}"),
        *Quote(EngineTimeResponseSchema), *Quote(RunId), *Quote(TimelineId),
        *Quote(EventId), *SlotCanonical, *Quote(RequestDigest), EngineTimeSec);
    return true;
  }

  bool EvidenceCapture(const FString &RequestJson, FString &OutResponseJson) {
    if (Utf8Bytes(RequestJson) > MaxEvidenceCaptureRequestBytes) {
      OutResponseJson =
          MakeError(TEXT("ANIMATION_EVIDENCE_REQUEST_TOO_LARGE"));
      return false;
    }
    TSharedPtr<FValue> Root;
    FString Error, Schema, KindName, ContextDigest;
    if (!Parse(RequestJson, Root, Error) ||
        !ExactKeys(*Root,
                   {TEXT("schema"), TEXT("kind"), TEXT("slot_binding"),
                    TEXT("context"), TEXT("context_digest")},
                   Error) ||
        !ReadRequiredString(*Root, TEXT("schema"), Schema) ||
        Schema != EvidenceCaptureRequestSchema ||
        !ReadRequiredString(*Root, TEXT("kind"), KindName) ||
        !ReadRequiredString(*Root, TEXT("context_digest"), ContextDigest) ||
        !IsLowerHex(ContextDigest, 64)) {
      OutResponseJson = MakeError(TEXT("ANIMATION_EVIDENCE_PROTOCOL_INVALID"));
      return false;
    }
    FVistaAnimationEvidenceCaptureInput Input;
    if (!ParseEvidenceKind(KindName, Input.Kind)) {
      OutResponseJson = MakeError(TEXT("ANIMATION_EVIDENCE_KIND_INVALID"));
      return false;
    }
    const FValue *SlotBinding = Field(*Root, TEXT("slot_binding"));
    const FValue *Context = Field(*Root, TEXT("context"));
    FString SlotCanonical;
    if (!SlotBinding || !Context ||
        !ValidateRuntimeSlotBinding(*SlotBinding, SlotCanonical) ||
        !ValidateEvidenceContext(*Context, Input)) {
      OutResponseJson =
          MakeError(TEXT("ANIMATION_EVIDENCE_CONTEXT_INVALID"));
      return false;
    }
    const FString DigestInput = FString::Printf(
        TEXT("{\"context\":%s,\"kind\":%s,\"schema\":%s,"
             "\"slot_binding\":%s}"),
        *Canonicalize(*Context), *Quote(KindName), *Quote(Schema),
        *SlotCanonical);
    if (Sha256HexUtf8(DigestInput) != ContextDigest) {
      OutResponseJson = MakeError(TEXT("ANIMATION_EVIDENCE_DIGEST_INVALID"));
      return false;
    }
    Input.ContextDigest = ContextDigest;

    TSharedPtr<IVistaAnimationContentDriver, ESPMode::ThreadSafe> LocalDriver;
    {
      FScopeLock Guard(&Mutex);
      const FActionContract *EvidenceAction =
          Input.Action.IsSet() ? FindAction(Input.Action.GetValue()) : nullptr;
      if (!bConfigured) {
        OutResponseJson = MakeError(TEXT("ANIMATION_CONTENT_API_UNCONFIGURED"));
        return false;
      }
      if (RuntimeSlotBindingJson != SlotCanonical ||
          Input.SceneRevision != Config.SlotBinding.SceneRevision) {
        OutResponseJson =
            MakeError(TEXT("ANIMATION_EVIDENCE_BINDING_MISMATCH"));
        return false;
      }
      if (!LastPreflight.bReady ||
          (Input.Action.IsSet() &&
           (!EvidenceAction || !TrustedAction(Input.Action.GetValue()) ||
            !LastPreflight.RequestedActions.Contains(
                EvidenceAction->Name))) ||
          (Input.ActorBindingId.IsSet() &&
           !LastPreflight.ActorBindings.Contains(
               Input.ActorBindingId.GetValue())) ||
          (Input.TargetBindingId.IsSet() &&
           !LastPreflight.TargetBindings.Contains(
               Input.TargetBindingId.GetValue()))) {
        OutResponseJson = MakeError(TEXT("ANIMATION_EVIDENCE_UNVERIFIED"));
        return false;
      }
      LocalDriver = Driver;
    }

    FVistaAnimationEvidenceCaptureOutput Output;
    FString DriverError;
    if (!LocalDriver ||
        !LocalDriver->CaptureEvidence(Input, Output, DriverError)) {
      OutResponseJson = MakeError(
          IsSafeErrorCode(DriverError)
              ? DriverError
              : TEXT("ANIMATION_EVIDENCE_DRIVER_FAILED"),
          true);
      return false;
    }
    const bool bAssertionRequired =
        Input.Kind == EVistaAnimationEvidenceKind::InteractionState ||
        Input.Kind == EVistaAnimationEvidenceKind::SceneValidation;
    const bool bAssertionProvided =
        Output.Assertion == EVistaAnimationEvidenceAssertion::Pass ||
        Output.Assertion == EVistaAnimationEvidenceAssertion::Fail;
    const bool bAssertionKnown =
        Output.Assertion ==
            EVistaAnimationEvidenceAssertion::NotApplicable ||
        bAssertionProvided;
    if (!IsOpaqueId(Output.EvidenceId) ||
        !IsSafeArtifactRef(Output.ArtifactRef) ||
        !IsLowerHex(Output.Sha256, 64) ||
        !bAssertionKnown || bAssertionRequired != bAssertionProvided) {
      OutResponseJson =
          MakeError(TEXT("ANIMATION_EVIDENCE_DRIVER_PROTOCOL_INVALID"));
      return false;
    }
    const TCHAR *AssertionJson = TEXT("null");
    if (Output.Assertion == EVistaAnimationEvidenceAssertion::Pass)
      AssertionJson = TEXT("\"pass\"");
    else if (Output.Assertion == EVistaAnimationEvidenceAssertion::Fail)
      AssertionJson = TEXT("\"fail\"");

    OutResponseJson = FString::Printf(
        TEXT("{\"schema\":%s,\"kind\":%s,\"context_digest\":%s,"
             "\"evidence\":{\"evidence_id\":%s,\"artifact_ref\":%s,"
             "\"sha256\":%s,\"assertion\":%s}}"),
        *Quote(EvidenceCaptureResponseSchema), *Quote(KindName),
        *Quote(ContextDigest), *Quote(Output.EvidenceId),
        *Quote(Output.ArtifactRef), *Quote(Output.Sha256), AssertionJson);
    return true;
  }

private:
  struct FEnvelope {
    const FOperationContract *Operation = nullptr;
    FString InvocationId;
    FString RequestDigest;
    FString Nonce;
    FString ContentProofCanonical;
    const FValue *Request = nullptr;
  };

  struct FPreflightState {
    bool bReady = false;
    TSet<FString> RequestedActions;
    TSet<FString> ActorBindings;
    TSet<FString> TargetBindings;
  };

  enum class EHandleState : uint8 {
    Started,
    Completed,
    Stopped,
    Released,
    Restored
  };

  struct FSnapshotState {
    EVistaAnimationAction Action = EVistaAnimationAction::Pause;
    FString ActorBindingId;
    TOptional<FString> TargetBindingId;
    FString SnapshotId;
    FString StateDigest;
    bool bStartAttempted = false;
    FString ActionHandle;
    bool bRestoreAttempted = false;
  };

  struct FHandleState {
    FString EventKey;
    EVistaAnimationAction Action = EVistaAnimationAction::Pause;
    EHandleState State = EHandleState::Started;
    bool bStopAttempted = false;
    bool bReleaseAttempted = false;
  };

  struct FJournalEntry {
    FString RequestDigest;
    FString Nonce;
    FString Response;
  };

  bool ValidateConfig(const FVistaAnimationTrustedRuntimeConfig &Candidate,
                      FString &OutError) const {
    const FVistaAnimationSlotBinding &Slot = Candidate.SlotBinding;
    const FVistaAnimationContentProof &Proof = Candidate.ContentProof;
    if (!IsOpaqueId(Slot.OwnerId) || !IsOpaqueId(Slot.SessionId) ||
        !IsOpaqueId(Slot.SlotId) || !IsOpaqueId(Slot.SceneRevision) ||
        !IsOpaqueId(Proof.ProfileId) || !IsOpaqueId(Proof.ProfileRevision) ||
        !IsOpaqueId(Proof.ContentRevision) ||
        !IsLowerHex(Proof.ContentDigest, 64) ||
        !IsOpaqueId(Proof.VerificationReceiptId) ||
        Candidate.Actions.IsEmpty() ||
        Candidate.Actions.Num() > UE_ARRAY_COUNT(Actions)) {
      OutError = TEXT("ANIMATION_TRUSTED_CONFIG_INVALID");
      return false;
    }
    TSet<EVistaAnimationAction> Seen;
    for (const FVistaAnimationTrustedAction &Action : Candidate.Actions) {
      const FActionContract *Fixed = FindAction(Action.Action);
      if (!Fixed || Action.AdapterId != Fixed->BridgeActionId ||
          Action.BridgeActionId != Fixed->BridgeActionId ||
          !IsSafeId(Action.CompletionSignal) || Action.TimeoutMs < 100 ||
          Action.TimeoutMs > 60000 || Seen.Contains(Action.Action)) {
        OutError = TEXT("ANIMATION_TRUSTED_CONFIG_INVALID");
        return false;
      }
      Seen.Add(Action.Action);
    }
    return true;
  }

  FString BuildSlotBindingJson(const FVistaAnimationSlotBinding &Slot) const {
    const FString Identity = FString::Printf(
        TEXT("{\"owner_id\":%s,\"scene_revision\":%s,\"schema\":%s,\"session_"
             "id\":%s,\"slot_id\":%s}"),
        *Quote(Slot.OwnerId), *Quote(Slot.SceneRevision), *Quote(SlotSchema),
        *Quote(Slot.SessionId), *Quote(Slot.SlotId));
    return FString::Printf(
        TEXT("{\"binding_digest\":%s,\"owner_id\":%s,\"scene_revision\":%s,"
             "\"schema\":%s,\"session_id\":%s,\"slot_id\":%s}"),
        *Quote(Sha256HexUtf8(Identity)), *Quote(Slot.OwnerId),
        *Quote(Slot.SceneRevision), *Quote(SlotSchema), *Quote(Slot.SessionId),
        *Quote(Slot.SlotId));
  }

  FString BuildRuntimeSlotBindingJson(
      const FVistaAnimationSlotBinding &Slot) const {
    return FString::Printf(
        TEXT("{\"owner_id\":%s,\"scene_revision\":%s,\"session_id\":%s,"
             "\"slot_id\":%s}"),
        *Quote(Slot.OwnerId), *Quote(Slot.SceneRevision),
        *Quote(Slot.SessionId), *Quote(Slot.SlotId));
  }

  bool ValidateRuntimeSlotBinding(const FValue &Value,
                                  FString &OutCanonical) const {
    FString Error, OwnerId, SessionId, SlotId, SceneRevision;
    if (!ExactKeys(Value,
                   {TEXT("owner_id"), TEXT("session_id"), TEXT("slot_id"),
                    TEXT("scene_revision")},
                   Error) ||
        !ReadRequiredString(Value, TEXT("owner_id"), OwnerId) ||
        !IsOpaqueId(OwnerId) ||
        !ReadRequiredString(Value, TEXT("session_id"), SessionId) ||
        !IsOpaqueId(SessionId) ||
        !ReadRequiredString(Value, TEXT("slot_id"), SlotId) ||
        !IsOpaqueId(SlotId) ||
        !ReadRequiredString(Value, TEXT("scene_revision"), SceneRevision) ||
        !IsOpaqueId(SceneRevision))
      return false;
    OutCanonical = Canonicalize(Value);
    return true;
  }

  bool ValidateEvidenceContext(
      const FValue &Context,
      FVistaAnimationEvidenceCaptureInput &OutInput) const {
    FString Error, Schema, PhaseName;
    TOptional<FString> ActionName;
    double AtFrameNumber = 0.0;
    double AttemptNumber = 0.0;
    if (!ExactKeys(
            Context,
            {TEXT("schema"), TEXT("run_id"), TEXT("timeline_id"),
             TEXT("scene_revision"), TEXT("event_id"), TEXT("action"),
             TEXT("actor_binding_id"), TEXT("target_binding_id"),
             TEXT("planned_sec"), TEXT("at_frame"), TEXT("attempt"),
             TEXT("phase"), TEXT("snapshot_id"), TEXT("action_handle")},
            Error) ||
        !ReadRequiredString(Context, TEXT("schema"), Schema) ||
        Schema != EvidenceContextSchema ||
        !ReadRequiredString(Context, TEXT("run_id"), OutInput.RunId) ||
        !IsOpaqueId(OutInput.RunId) ||
        !ReadRequiredString(Context, TEXT("timeline_id"),
                            OutInput.TimelineId) ||
        !IsTimelineId(OutInput.TimelineId) ||
        !ReadRequiredString(Context, TEXT("scene_revision"),
                            OutInput.SceneRevision) ||
        !IsOpaqueId(OutInput.SceneRevision) ||
        !ReadRequiredNullableString(Context, TEXT("event_id"),
                                    OutInput.EventId) ||
        (OutInput.EventId.IsSet() &&
         !IsEventId(OutInput.EventId.GetValue())) ||
        !ReadRequiredNullableString(Context, TEXT("action"), ActionName) ||
        !ReadRequiredNullableString(Context, TEXT("actor_binding_id"),
                                    OutInput.ActorBindingId) ||
        (OutInput.ActorBindingId.IsSet() &&
         !IsSafeId(OutInput.ActorBindingId.GetValue())) ||
        !ReadRequiredNullableString(Context, TEXT("target_binding_id"),
                                    OutInput.TargetBindingId) ||
        (OutInput.TargetBindingId.IsSet() &&
         !IsSafeId(OutInput.TargetBindingId.GetValue())) ||
        !ReadRequiredNumber(Context, TEXT("planned_sec"),
                            OutInput.PlannedSec) ||
        OutInput.PlannedSec < 0.0 || OutInput.PlannedSec > 3600.0 ||
        !ReadRequiredNumber(Context, TEXT("at_frame"), AtFrameNumber) ||
        AtFrameNumber < 0.0 || AtFrameNumber > 864000.0 ||
        !ReadRequiredNumber(Context, TEXT("attempt"), AttemptNumber) ||
        AttemptNumber < 0.0 || AttemptNumber > 100.0 ||
        !ReadRequiredString(Context, TEXT("phase"), PhaseName) ||
        !ParseEvidencePhase(PhaseName, OutInput.Phase) ||
        !ReadRequiredNullableString(Context, TEXT("snapshot_id"),
                                    OutInput.SnapshotId) ||
        (OutInput.SnapshotId.IsSet() &&
         !IsOpaqueId(OutInput.SnapshotId.GetValue())) ||
        !ReadRequiredNullableString(Context, TEXT("action_handle"),
                                    OutInput.ActionHandle) ||
        (OutInput.ActionHandle.IsSet() &&
         !IsOpaqueId(OutInput.ActionHandle.GetValue())))
      return false;
    OutInput.AtFrame = static_cast<int32>(AtFrameNumber);
    OutInput.Attempt = static_cast<int32>(AttemptNumber);
    if (AtFrameNumber != static_cast<double>(OutInput.AtFrame) ||
        AttemptNumber != static_cast<double>(OutInput.Attempt))
      return false;

    const FActionContract *Action = nullptr;
    if (ActionName.IsSet()) {
      Action = FindActionByName(ActionName.GetValue());
      if (!Action)
        return false;
      OutInput.Action = Action->Action;
      if (!ValidateTargetPolicy(*Action, OutInput.TargetBindingId))
        return false;
    }

    const bool bTerminal =
        OutInput.Phase == EVistaAnimationEvidencePhase::Terminal;
    if (bTerminal) {
      if (OutInput.EventId.IsSet() || OutInput.Action.IsSet() ||
          OutInput.ActorBindingId.IsSet() ||
          OutInput.TargetBindingId.IsSet() || OutInput.SnapshotId.IsSet() ||
          OutInput.ActionHandle.IsSet() || OutInput.Attempt != 0 ||
          OutInput.Kind == EVistaAnimationEvidenceKind::InteractionState)
        return false;
    } else {
      if (!OutInput.EventId.IsSet() || !OutInput.Action.IsSet() ||
          !OutInput.ActorBindingId.IsSet() || !OutInput.SnapshotId.IsSet() ||
          OutInput.Attempt < 1 ||
          OutInput.Kind == EVistaAnimationEvidenceKind::SceneValidation)
        return false;
      if (OutInput.Phase == EVistaAnimationEvidencePhase::Before &&
          OutInput.ActionHandle.IsSet())
        return false;
      if (OutInput.Phase == EVistaAnimationEvidencePhase::After &&
          !OutInput.ActionHandle.IsSet())
        return false;
      if (OutInput.Kind == EVistaAnimationEvidenceKind::InteractionState &&
          !OutInput.TargetBindingId.IsSet())
        return false;
      if (OutInput.Kind == EVistaAnimationEvidenceKind::Screenshot &&
          OutInput.Phase == EVistaAnimationEvidencePhase::Before)
        return false;
    }
    if (OutInput.Kind == EVistaAnimationEvidenceKind::SceneValidation &&
        !bTerminal)
      return false;
    return true;
  }

  FString
  BuildContentProofJson(const FVistaAnimationContentProof &Proof) const {
    return FString::Printf(
        TEXT("{\"content_digest\":%s,\"content_revision\":%s,\"profile_id\":%s,"
             "\"profile_revision\":%s,\"schema\":%s,\"verification_receipt_"
             "id\":%s}"),
        *Quote(Proof.ContentDigest), *Quote(Proof.ContentRevision),
        *Quote(Proof.ProfileId), *Quote(Proof.ProfileRevision),
        *Quote(ContentProofSchema), *Quote(Proof.VerificationReceiptId));
  }

  bool ValidateNonceMarker(const FValue &Value, FString &OutNonce) const {
    FString Error, Schema;
    return ExactKeys(Value, {TEXT("schema"), TEXT("nonce")}, Error) &&
           ReadRequiredString(Value, TEXT("schema"), Schema) &&
           Schema == NonceSchema &&
           ReadRequiredString(Value, TEXT("nonce"), OutNonce) &&
           IsLowerHex(OutNonce, 32);
  }

  bool ValidateSlotBinding(const FValue &Value) const {
    FString Error, Schema, Owner, Session, Slot, Scene, BindingDigest;
    if (!ExactKeys(Value,
                   {TEXT("schema"), TEXT("owner_id"), TEXT("session_id"),
                    TEXT("slot_id"), TEXT("scene_revision"),
                    TEXT("binding_digest")},
                   Error) ||
        !ReadRequiredString(Value, TEXT("schema"), Schema) ||
        Schema != SlotSchema ||
        !ReadRequiredString(Value, TEXT("owner_id"), Owner) ||
        !IsOpaqueId(Owner) ||
        !ReadRequiredString(Value, TEXT("session_id"), Session) ||
        !IsOpaqueId(Session) ||
        !ReadRequiredString(Value, TEXT("slot_id"), Slot) ||
        !IsOpaqueId(Slot) ||
        !ReadRequiredString(Value, TEXT("scene_revision"), Scene) ||
        !IsOpaqueId(Scene) ||
        !ReadRequiredString(Value, TEXT("binding_digest"), BindingDigest) ||
        !IsLowerHex(BindingDigest, 64))
      return false;
    const FString Identity =
        FString::Printf(TEXT("{\"owner_id\":%s,\"scene_revision\":%s,"
                             "\"schema\":%s,\"session_id\":%s,\"slot_id\":%s}"),
                        *Quote(Owner), *Quote(Scene), *Quote(Schema),
                        *Quote(Session), *Quote(Slot));
    return Sha256HexUtf8(Identity) == BindingDigest;
  }

  bool ValidateContentProof(const FValue &Value) const {
    FString Error, Schema, ProfileId, ProfileRevision, ContentRevision,
        ContentDigest, Receipt;
    return ExactKeys(Value,
                     {TEXT("schema"), TEXT("profile_id"),
                      TEXT("profile_revision"), TEXT("content_revision"),
                      TEXT("content_digest"), TEXT("verification_receipt_id")},
                     Error) &&
           ReadRequiredString(Value, TEXT("schema"), Schema) &&
           Schema == ContentProofSchema &&
           ReadRequiredString(Value, TEXT("profile_id"), ProfileId) &&
           IsOpaqueId(ProfileId) &&
           ReadRequiredString(Value, TEXT("profile_revision"),
                              ProfileRevision) &&
           IsOpaqueId(ProfileRevision) &&
           ReadRequiredString(Value, TEXT("content_revision"),
                              ContentRevision) &&
           IsOpaqueId(ContentRevision) &&
           ReadRequiredString(Value, TEXT("content_digest"), ContentDigest) &&
           IsLowerHex(ContentDigest, 64) &&
           ReadRequiredString(Value, TEXT("verification_receipt_id"),
                              Receipt) &&
           IsOpaqueId(Receipt);
  }

  bool ValidateEnvelope(const FValue &Root, FEnvelope &Out,
                        FString &OutResponse) const {
    FString Schema, OperationId, Fingerprint;
    if (!ReadRequiredString(Root, TEXT("schema"), Schema) ||
        Schema != RequestEnvelopeSchema ||
        !ReadRequiredString(Root, TEXT("operation_id"), OperationId) ||
        !ReadRequiredString(Root, TEXT("operation_fingerprint"), Fingerprint) ||
        !ReadRequiredString(Root, TEXT("invocation_id"), Out.InvocationId) ||
        !ReadRequiredString(Root, TEXT("request_digest"), Out.RequestDigest) ||
        !IsLowerHex(Out.RequestDigest, 64)) {
      OutResponse = MakeError(TEXT("ANIMATION_CONTENT_IDENTITY_INVALID"));
      return false;
    }
    Out.Operation = FindOperation(OperationId);
    if (!Out.Operation || Fingerprint != Out.Operation->Fingerprint ||
        !IsOpaqueId(Out.InvocationId)) {
      OutResponse = MakeError(TEXT("ANIMATION_OPERATION_FORBIDDEN"));
      return false;
    }
    const FValue *NonceMarker = Field(Root, TEXT("nonce_marker"));
    const FValue *ContentProof = Field(Root, TEXT("content_proof"));
    Out.Request = Field(Root, TEXT("request"));
    if (!NonceMarker || !ContentProof || !Out.Request ||
        !ValidateNonceMarker(*NonceMarker, Out.Nonce) ||
        !ValidateContentProof(*ContentProof)) {
      OutResponse = MakeError(TEXT("ANIMATION_CONTENT_CORRELATION_INVALID"));
      return false;
    }
    const FString OperationName =
        OperationId.Mid(FCString::Strlen(TEXT("vista.animation.")));
    const FString ShortName =
        OperationName.LeftChop(FCString::Strlen(TEXT(".v1")));
    const FString ExpectedInvocation =
        FString::Printf(TEXT("vau-%s-%s-%s"), *ShortName, *Out.Nonce.Left(16),
                        *Out.RequestDigest.Left(12));
    if (Out.InvocationId != ExpectedInvocation) {
      OutResponse = MakeError(TEXT("ANIMATION_INVOCATION_INVALID"));
      return false;
    }
    Out.ContentProofCanonical = Canonicalize(*ContentProof);
    const FString DigestInput = FString::Printf(
        TEXT("{\"content_proof\":%s,\"request\":%s}"),
        *Out.ContentProofCanonical, *Canonicalize(*Out.Request));
    if (Sha256HexUtf8(DigestInput) != Out.RequestDigest) {
      OutResponse = MakeError(TEXT("ANIMATION_REQUEST_DIGEST_INVALID"));
      return false;
    }
    return true;
  }

  bool ConsumeNonceLocked(const FString &Nonce) {
    if (UsedNonces.Contains(Nonce))
      return false;
    UsedNonces.Add(Nonce);
    NonceOrder.Add(Nonce);
    if (NonceOrder.Num() > MaxReplayEntries) {
      UsedNonces.Remove(NonceOrder[0]);
      NonceOrder.RemoveAt(0, 1, EAllowShrinking::No);
    }
    return true;
  }

  FString BuildCapabilityResponse(const FString &Nonce,
                                  const FString &ChallengeDigest,
                                  const FString &Slot, const FString &Content,
                                  const FString &Process,
                                  const FString &BinarySha) const {
    TArray<FString> OperationJson;
    for (const FOperationContract &Operation : Operations) {
      OperationJson.Add(FString::Printf(
          TEXT("{\"operation_id\":%s,\"operation_fingerprint\":%s,\"request_"
               "schema\":%s,\"response_schema\":%s,\"mutation\":%s,\"max_"
               "attempts\":%d}"),
          *Quote(Operation.OperationId), *Quote(Operation.Fingerprint),
          *Quote(Operation.RequestSchema), *Quote(Operation.ResponseSchema),
          Operation.bMutation ? TEXT("true") : TEXT("false"),
          Operation.MaxAttempts));
    }
    const FString Artifact = FString::Printf(
        TEXT("{\"schema\":\"vista-animation-ue-plugin-artifact/"
             "v1\",\"plugin_name\":\"VistaAnimationContentApi\",\"plugin_"
             "version\":%s,\"plugin_build_id\":%s,\"binary_sha256\":%s,"
             "\"engine_version\":%s,\"target_platform\":%s,\"api_schema\":"
             "\"vista-animation-ue-content-api/v1\"}"),
        *Quote(VISTA_ANIMATION_PLUGIN_VERSION),
        *Quote(VISTA_ANIMATION_PLUGIN_BUILD_ID), *Quote(BinarySha),
        *Quote(EngineIdentity()), *Quote(PlatformIdentity()));
    const FString Security =
        TEXT("{\"schema\":\"vista-animation-ue-security-policy/"
             "v1\",\"json_only\":true,\"fixed_operation_allowlist\":true,"
             "\"nonce_echo_required\":true,\"request_digest_echo_required\":"
             "true,\"slot_binding_required\":true,\"mutation_max_attempts\":1,"
             "\"caller_python\":false,\"caller_console\":false,\"caller_"
             "script\":false,\"caller_asset_paths\":false}");
    return FString::Printf(
        TEXT("{\"schema\":%s,\"status\":\"ready\",\"operation_id\":%s,"
             "\"operation_fingerprint\":%s,\"nonce_marker\":{\"schema\":%s,"
             "\"nonce\":%s},\"challenge_digest\":%s,\"slot_binding\":%s,"
             "\"plugin_artifact\":%s,\"process_instance_id\":%s,\"content_"
             "proof\":%s,\"security\":%s,\"operation_allowlist_digest\":%s,"
             "\"operations\":[%s]}"),
        *Quote(CapabilityResponseSchema), *Quote(CapabilityOperationId),
        *Quote(CapabilityFingerprint), *Quote(NonceSchema), *Quote(Nonce),
        *Quote(ChallengeDigest), *Slot, *Artifact, *Quote(Process), *Content,
        *Security, *Quote(OperationAllowlistDigest),
        *FString::Join(OperationJson, TEXT(",")));
  }

  FString BuildEnvelopeResponse(const FEnvelope &Envelope,
                                const FString &Payload) const {
    return FString::Printf(
        TEXT("{\"schema\":%s,\"operation_id\":%s,\"operation_fingerprint\":%s,"
             "\"invocation_id\":%s,\"request_digest\":%s,\"nonce_marker\":{"
             "\"schema\":%s,\"nonce\":%s},\"payload\":%s}"),
        *Quote(ResponseEnvelopeSchema), *Quote(Envelope.Operation->OperationId),
        *Quote(Envelope.Operation->Fingerprint), *Quote(Envelope.InvocationId),
        *Quote(Envelope.RequestDigest), *Quote(NonceSchema),
        *Quote(Envelope.Nonce), *Payload);
  }

  bool BeginMutation(const FEnvelope &Envelope, FString &OutError) {
    FScopeLock Guard(&Mutex);
    if (Journal.Contains(Envelope.InvocationId)) {
      OutError = MakeError(TEXT("ANIMATION_MUTATION_OUTCOME_UNKNOWN"));
      return false;
    }
    while (Journal.Num() >= MaxJournalEntries && !JournalOrder.IsEmpty()) {
      const FString Oldest = JournalOrder[0];
      const FJournalEntry *Entry = Journal.Find(Oldest);
      if (Entry && Entry->Response.IsEmpty())
        break;
      Journal.Remove(Oldest);
      JournalOrder.RemoveAt(0, 1, EAllowShrinking::No);
    }
    if (Journal.Num() >= MaxJournalEntries) {
      OutError = MakeError(TEXT("ANIMATION_MUTATION_JOURNAL_FULL"));
      return false;
    }
    Journal.Add(Envelope.InvocationId,
                {Envelope.RequestDigest, Envelope.Nonce, FString()});
    JournalOrder.Add(Envelope.InvocationId);
    return true;
  }

  void CompleteMutation(const FEnvelope &Envelope, const FString &Response) {
    FScopeLock Guard(&Mutex);
    if (FJournalEntry *Entry = Journal.Find(Envelope.InvocationId))
      Entry->Response = Response;
  }

  const FVistaAnimationTrustedAction *
  TrustedAction(EVistaAnimationAction Action) const {
    for (const FVistaAnimationTrustedAction &Candidate : Config.Actions)
      if (Candidate.Action == Action)
        return &Candidate;
    return nullptr;
  }

  bool
  ValidateLifecycleIdentity(const FValue &Request,
                            const FOperationContract &Operation,
                            std::initializer_list<const TCHAR *> AdditionalKeys,
                            FString &OutPreflightId, FString &OutRunId,
                            FString &OutEventId, FString &OutError) const {
    TArray<const TCHAR *> Keys = {TEXT("schema"), TEXT("preflight_id"),
                                  TEXT("run_id"), TEXT("event_id")};
    for (const TCHAR *Key : AdditionalKeys)
      Keys.Add(Key);
    if (Request.Kind != EKind::Object || Request.Object.Num() != Keys.Num()) {
      OutError = MakeError(TEXT("ANIMATION_REQUEST_SHAPE_INVALID"));
      return false;
    }
    for (const TCHAR *Key : Keys)
      if (!Request.Object.Contains(Key)) {
        OutError = MakeError(TEXT("ANIMATION_REQUEST_SHAPE_INVALID"));
        return false;
      }
    FString Schema;
    if (!ReadRequiredString(Request, TEXT("schema"), Schema) ||
        Schema != Operation.RequestSchema ||
        !ReadRequiredString(Request, TEXT("preflight_id"), OutPreflightId) ||
        !IsPreflightId(OutPreflightId) ||
        !ReadRequiredString(Request, TEXT("run_id"), OutRunId) ||
        !IsOpaqueId(OutRunId) ||
        !ReadRequiredString(Request, TEXT("event_id"), OutEventId) ||
        !IsEventId(OutEventId)) {
      OutError = MakeError(TEXT("ANIMATION_LIFECYCLE_IDENTITY_INVALID"));
      return false;
    }
    return true;
  }

  bool BindPreflightLocked(const FString &PreflightId) {
    if (!LastPreflight.bReady)
      return false;
    if (ActivePreflightId.IsEmpty())
      ActivePreflightId = PreflightId;
    return ActivePreflightId == PreflightId;
  }

  bool ValidateTargetPolicy(const FActionContract &Action,
                            const TOptional<FString> &Target) const {
    if (Action.TargetPolicy == FActionContract::ETargetPolicy::Required &&
        !Target.IsSet())
      return false;
    if (Action.TargetPolicy == FActionContract::ETargetPolicy::Forbidden &&
        Target.IsSet())
      return false;
    return true;
  }

  bool HandlePreflight(const FEnvelope &Envelope, FString &OutPayload);
  bool HandleSnapshot(const FEnvelope &Envelope, FString &OutPayload);
  bool HandleStart(const FEnvelope &Envelope, FString &OutPayload);
  bool HandleWait(const FEnvelope &Envelope, FString &OutPayload);
  bool HandleStop(const FEnvelope &Envelope, FString &OutPayload);
  bool HandleRelease(const FEnvelope &Envelope, FString &OutPayload);
  bool HandleRestore(const FEnvelope &Envelope, FString &OutPayload);

  FCriticalSection Mutex;
  bool bConfigured = false;
  FVistaAnimationTrustedRuntimeConfig Config;
  TSharedPtr<IVistaAnimationContentDriver, ESPMode::ThreadSafe> Driver;
  FString ProcessInstanceId;
  FString SlotBindingJson;
  FString RuntimeSlotBindingJson;
  FString ContentProofJson;
  const double ProcessMonotonicOriginSec = FPlatformTime::Seconds();
  double LastEngineTimeSec = 0.0;
  TSet<FString> UsedNonces;
  TArray<FString> NonceOrder;
  TMap<FString, FJournalEntry> Journal;
  TArray<FString> JournalOrder;
  FPreflightState LastPreflight;
  bool bPreflightInProgress = false;
  FString ActivePreflightId;
  TMap<FString, FSnapshotState> Snapshots;
  TMap<FString, FHandleState> Handles;
};

UVistaAnimationContentApiSubsystem::UVistaAnimationContentApiSubsystem() =
    default;

UVistaAnimationContentApiSubsystem::~UVistaAnimationContentApiSubsystem() =
    default;

void UVistaAnimationContentApiSubsystem::FImplementationDeleter::operator()(
    FImplementation *Instance) const {
  delete Instance;
}

bool UVistaAnimationContentApiSubsystem::FImplementation::HandlePreflight(
    const FEnvelope &Envelope, FString &OutPayload) {
  const FValue &Request = *Envelope.Request;
  FString Error, Schema, SceneRevision, ProfileId, ProfileRevision,
      ContentRevision, ContentDigest;
  if (!ExactKeys(Request,
                 {TEXT("schema"), TEXT("scene_revision"), TEXT("profile_id"),
                  TEXT("profile_revision"), TEXT("content_revision"),
                  TEXT("content_digest"), TEXT("requested_actions"),
                  TEXT("actor_binding_ids"), TEXT("target_binding_ids")},
                 Error) ||
      !ReadRequiredString(Request, TEXT("schema"), Schema) ||
      Schema != Envelope.Operation->RequestSchema ||
      !ReadRequiredString(Request, TEXT("scene_revision"), SceneRevision) ||
      !IsOpaqueId(SceneRevision) ||
      !ReadRequiredString(Request, TEXT("profile_id"), ProfileId) ||
      !IsOpaqueId(ProfileId) ||
      !ReadRequiredString(Request, TEXT("profile_revision"), ProfileRevision) ||
      !IsOpaqueId(ProfileRevision) ||
      !ReadRequiredString(Request, TEXT("content_revision"), ContentRevision) ||
      !IsOpaqueId(ContentRevision) ||
      !ReadRequiredString(Request, TEXT("content_digest"), ContentDigest) ||
      !IsLowerHex(ContentDigest, 64)) {
    OutPayload = MakeError(TEXT("ANIMATION_PREFLIGHT_REQUEST_INVALID"));
    return false;
  }

  TArray<FString> RequestedNames, ActorBindings, TargetBindings;
  if (!ReadSafeStringArray(Request, TEXT("requested_actions"), RequestedNames,
                           false) ||
      !ReadSafeStringArray(Request, TEXT("actor_binding_ids"), ActorBindings,
                           false) ||
      !ReadSafeStringArray(Request, TEXT("target_binding_ids"), TargetBindings,
                           true)) {
    OutPayload = MakeError(TEXT("ANIMATION_PREFLIGHT_REQUEST_INVALID"));
    return false;
  }

  FVistaAnimationPreflightInput Input;
  Input.SceneRevision = SceneRevision;
  Input.ActorBindingIds = ActorBindings;
  Input.TargetBindingIds = TargetBindings;
  TSharedPtr<IVistaAnimationContentDriver, ESPMode::ThreadSafe> LocalDriver;
  {
    FScopeLock Guard(&Mutex);
    if (!bConfigured || SceneRevision != Config.SlotBinding.SceneRevision ||
        ProfileId != Config.ContentProof.ProfileId ||
        ProfileRevision != Config.ContentProof.ProfileRevision ||
        ContentRevision != Config.ContentProof.ContentRevision ||
        ContentDigest != Config.ContentProof.ContentDigest ||
        !Snapshots.IsEmpty() || bPreflightInProgress) {
      OutPayload = MakeError(TEXT("ANIMATION_PREFLIGHT_PROVENANCE_MISMATCH"));
      return false;
    }
    for (const FString &Name : RequestedNames) {
      const FActionContract *Action = FindActionByName(Name);
      if (!Action || !TrustedAction(Action->Action)) {
        OutPayload = MakeError(TEXT("ANIMATION_ACTION_UNVERIFIED"));
        return false;
      }
      Input.RequestedActions.Add(Action->Action);
    }
    bPreflightInProgress = true;
    LocalDriver = Driver;
  }

  struct FPreflightReservation final {
    FCriticalSection &Mutex;
    bool &Flag;
    ~FPreflightReservation() {
      FScopeLock Guard(&Mutex);
      Flag = false;
    }
  } Reservation{Mutex, bPreflightInProgress};

  FVistaAnimationPreflightOutput Output;
  FString DriverError;
  if (!LocalDriver || !LocalDriver->Preflight(Input, Output, DriverError)) {
    OutPayload = MakeError(IsSafeErrorCode(DriverError)
                               ? DriverError
                               : TEXT("ANIMATION_PREFLIGHT_DRIVER_FAILED"),
                           true);
    return false;
  }

  TSet<FString> ExpectedActors;
  TSet<FString> ExpectedTargets;
  TSet<EVistaAnimationAction> ExpectedActions;
  for (const FString &Value : ActorBindings)
    ExpectedActors.Add(Value);
  for (const FString &Value : TargetBindings)
    ExpectedTargets.Add(Value);
  for (EVistaAnimationAction Value : Input.RequestedActions)
    ExpectedActions.Add(Value);
  TSet<FString> SeenActors, SeenTargets;
  TSet<EVistaAnimationAction> SeenActions;
  TArray<FVistaAnimationRuntimeBinding> SortedActors = Output.Actors;
  TArray<FVistaAnimationRuntimeBinding> SortedTargets = Output.Targets;
  TArray<FVistaAnimationActionAvailability> SortedActions = Output.Actions;
  SortedActors.Sort([](const auto &Left, const auto &Right) {
    return Left.BindingId < Right.BindingId;
  });
  SortedTargets.Sort([](const auto &Left, const auto &Right) {
    return Left.BindingId < Right.BindingId;
  });
  SortedActions.Sort([](const auto &Left, const auto &Right) {
    const FActionContract *L = FindAction(Left.Action);
    const FActionContract *R = FindAction(Right.Action);
    return L && R && FString(L->Name) < FString(R->Name);
  });

  auto ValidateBinding = [](FVistaAnimationRuntimeBinding &Binding,
                            const TSet<FString> &Expected,
                            TSet<FString> &Seen) {
    if (!Expected.Contains(Binding.BindingId) ||
        Seen.Contains(Binding.BindingId) || !IsSafeId(Binding.BindingId))
      return false;
    Binding.Capabilities.Sort();
    Binding.AnchorKinds.Sort();
    for (int32 Index = 0; Index < Binding.Capabilities.Num(); ++Index) {
      if (!IsSafeId(Binding.Capabilities[Index]) ||
          (Index > 0 &&
           Binding.Capabilities[Index] == Binding.Capabilities[Index - 1]))
        return false;
    }
    for (int32 Index = 0; Index < Binding.AnchorKinds.Num(); ++Index) {
      if (!IsSafeId(Binding.AnchorKinds[Index]) ||
          (Index > 0 &&
           Binding.AnchorKinds[Index] == Binding.AnchorKinds[Index - 1]))
        return false;
    }
    Seen.Add(Binding.BindingId);
    return true;
  };
  for (FVistaAnimationRuntimeBinding &Binding : SortedActors)
    if (!ValidateBinding(Binding, ExpectedActors, SeenActors)) {
      OutPayload =
          MakeError(TEXT("ANIMATION_PREFLIGHT_DRIVER_PROTOCOL_INVALID"));
      return false;
    }
  for (FVistaAnimationRuntimeBinding &Binding : SortedTargets)
    if (!ValidateBinding(Binding, ExpectedTargets, SeenTargets)) {
      OutPayload =
          MakeError(TEXT("ANIMATION_PREFLIGHT_DRIVER_PROTOCOL_INVALID"));
      return false;
    }
  if (SeenActors.Num() != ExpectedActors.Num() ||
      SeenTargets.Num() != ExpectedTargets.Num()) {
    OutPayload = MakeError(TEXT("ANIMATION_PREFLIGHT_DRIVER_PROTOCOL_INVALID"));
    return false;
  }
  for (const FVistaAnimationActionAvailability &Availability : SortedActions) {
    if (!ExpectedActions.Contains(Availability.Action) ||
        SeenActions.Contains(Availability.Action) ||
        !FindAction(Availability.Action)) {
      OutPayload =
          MakeError(TEXT("ANIMATION_PREFLIGHT_DRIVER_PROTOCOL_INVALID"));
      return false;
    }
    SeenActions.Add(Availability.Action);
  }
  if (SeenActions.Num() != ExpectedActions.Num()) {
    OutPayload = MakeError(TEXT("ANIMATION_PREFLIGHT_DRIVER_PROTOCOL_INVALID"));
    return false;
  }

  bool bReady = true;
  TArray<FString> ActorJson, TargetJson, ActionJson;
  for (const FVistaAnimationRuntimeBinding &Binding : SortedActors) {
    bReady = bReady && Binding.bAvailable && Binding.bClassMatches &&
             Binding.bSkeletonMatches;
    ActorJson.Add(FString::Printf(
        TEXT(
            "{\"binding_id\":%s,\"available\":%s,\"class_matches\":%s,"
            "\"skeleton_matches\":%s,\"capabilities\":%s,\"anchor_kinds\":%s}"),
        *Quote(Binding.BindingId),
        Binding.bAvailable ? TEXT("true") : TEXT("false"),
        Binding.bClassMatches ? TEXT("true") : TEXT("false"),
        Binding.bSkeletonMatches ? TEXT("true") : TEXT("false"),
        *JsonStringArray(Binding.Capabilities),
        *JsonStringArray(Binding.AnchorKinds)));
  }
  for (const FVistaAnimationRuntimeBinding &Binding : SortedTargets) {
    bReady = bReady && Binding.bAvailable && Binding.bClassMatches;
    TargetJson.Add(FString::Printf(
        TEXT(
            "{\"binding_id\":%s,\"available\":%s,\"class_matches\":%s,"
            "\"skeleton_matches\":%s,\"capabilities\":%s,\"anchor_kinds\":%s}"),
        *Quote(Binding.BindingId),
        Binding.bAvailable ? TEXT("true") : TEXT("false"),
        Binding.bClassMatches ? TEXT("true") : TEXT("false"),
        Binding.bSkeletonMatches ? TEXT("true") : TEXT("false"),
        *JsonStringArray(Binding.Capabilities),
        *JsonStringArray(Binding.AnchorKinds)));
  }
  for (const FVistaAnimationActionAvailability &Availability : SortedActions) {
    const FActionContract &Action = *FindAction(Availability.Action);
    bReady = bReady && Availability.bAvailable &&
             Availability.bImplementationMatches &&
             Availability.bCompletionSignalAvailable;
    ActionJson.Add(FString::Printf(
        TEXT("{\"action\":%s,\"bridge_action_id\":%s,\"available\":%s,"
             "\"implementation_matches\":%s,\"completion_signal_available\":%"
             "s}"),
        *Quote(Action.Name), *Quote(Action.BridgeActionId),
        Availability.bAvailable ? TEXT("true") : TEXT("false"),
        Availability.bImplementationMatches ? TEXT("true") : TEXT("false"),
        Availability.bCompletionSignalAvailable ? TEXT("true")
                                                : TEXT("false")));
  }

  OutPayload = FString::Printf(
      TEXT("{\"schema\":%s,\"scene_revision\":%s,\"profile_revision\":%s,"
           "\"content_revision\":%s,\"content_digest\":%s,\"ready\":%s,"
           "\"actors\":[%s],\"targets\":[%s],\"actions\":[%s]}"),
      *Quote(Envelope.Operation->ResponseSchema), *Quote(SceneRevision),
      *Quote(ProfileRevision), *Quote(ContentRevision), *Quote(ContentDigest),
      bReady ? TEXT("true") : TEXT("false"),
      *FString::Join(ActorJson, TEXT(",")),
      *FString::Join(TargetJson, TEXT(",")),
      *FString::Join(ActionJson, TEXT(",")));
  {
    FScopeLock Guard(&Mutex);
    if (!Snapshots.IsEmpty()) {
      OutPayload = MakeError(TEXT("ANIMATION_PREFLIGHT_STATE_CONFLICT"));
      return false;
    }
    LastPreflight.bReady = bReady;
    LastPreflight.RequestedActions.Reset();
    for (const FString &Name : RequestedNames)
      LastPreflight.RequestedActions.Add(Name);
    LastPreflight.ActorBindings = MoveTemp(ExpectedActors);
    LastPreflight.TargetBindings = MoveTemp(ExpectedTargets);
    ActivePreflightId.Reset();
    Handles.Reset();
  }
  return true;
}

bool UVistaAnimationContentApiSubsystem::FImplementation::HandleSnapshot(
    const FEnvelope &Envelope, FString &OutPayload) {
  const FValue &Request = *Envelope.Request;
  FString PreflightId, RunId, EventId, ActionName, ActorBinding;
  TOptional<FString> TargetBinding;
  if (!ValidateLifecycleIdentity(
          Request, *Envelope.Operation,
          {TEXT("action"), TEXT("actor_binding_id"), TEXT("target_binding_id")},
          PreflightId, RunId, EventId, OutPayload) ||
      !ReadRequiredString(Request, TEXT("action"), ActionName) ||
      !ReadRequiredString(Request, TEXT("actor_binding_id"), ActorBinding) ||
      !IsSafeId(ActorBinding) ||
      !ReadRequiredNullableString(Request, TEXT("target_binding_id"),
                                  TargetBinding) ||
      (TargetBinding.IsSet() && !IsSafeId(TargetBinding.GetValue()))) {
    if (OutPayload.IsEmpty())
      OutPayload = MakeError(TEXT("ANIMATION_SNAPSHOT_REQUEST_INVALID"));
    return false;
  }
  const FActionContract *Action = FindActionByName(ActionName);
  if (!Action || !ValidateTargetPolicy(*Action, TargetBinding)) {
    OutPayload = MakeError(TEXT("ANIMATION_SNAPSHOT_REQUEST_INVALID"));
    return false;
  }
  const FString Key = EventKey(PreflightId, RunId, EventId);
  TSharedPtr<IVistaAnimationContentDriver, ESPMode::ThreadSafe> LocalDriver;
  {
    FScopeLock Guard(&Mutex);
    if (bPreflightInProgress || !BindPreflightLocked(PreflightId) ||
        !LastPreflight.RequestedActions.Contains(ActionName) ||
        !LastPreflight.ActorBindings.Contains(ActorBinding) ||
        (TargetBinding.IsSet() &&
         !LastPreflight.TargetBindings.Contains(TargetBinding.GetValue())) ||
        Snapshots.Contains(Key) || Snapshots.Num() >= MaxLifecycleEntries ||
        Handles.Num() >= MaxLifecycleEntries) {
      OutPayload = MakeError(TEXT("ANIMATION_SNAPSHOT_STATE_CONFLICT"));
      return false;
    }
    LocalDriver = Driver;
  }
  FVistaAnimationSnapshotOutput Output;
  FString DriverError;
  if (!LocalDriver ||
      !LocalDriver->Snapshot(Action->Action, ActorBinding, TargetBinding,
                             Output, DriverError)) {
    OutPayload = MakeError(IsSafeErrorCode(DriverError)
                               ? DriverError
                               : TEXT("ANIMATION_SNAPSHOT_DRIVER_FAILED"),
                           true);
    return false;
  }
  if (!IsOpaqueId(Output.SnapshotId) || !IsLowerHex(Output.StateDigest, 64) ||
      !FMath::IsFinite(Output.EngineTimeSec) || Output.EngineTimeSec < 0 ||
      Output.EngineTimeSec > MaxEngineTimeSec) {
    OutPayload = MakeError(TEXT("ANIMATION_SNAPSHOT_DRIVER_PROTOCOL_INVALID"));
    return false;
  }
  {
    FScopeLock Guard(&Mutex);
    if (Snapshots.Contains(Key) || Snapshots.Num() >= MaxLifecycleEntries) {
      OutPayload = MakeError(TEXT("ANIMATION_SNAPSHOT_STATE_CONFLICT"));
      return false;
    }
    for (const auto &Entry : Snapshots)
      if (Entry.Value.SnapshotId == Output.SnapshotId) {
        OutPayload = MakeError(TEXT("ANIMATION_SNAPSHOT_ID_REUSED"));
        return false;
      }
    FSnapshotState State;
    State.Action = Action->Action;
    State.ActorBindingId = ActorBinding;
    State.TargetBindingId = TargetBinding;
    State.SnapshotId = Output.SnapshotId;
    State.StateDigest = Output.StateDigest;
    Snapshots.Add(Key, MoveTemp(State));
  }
  OutPayload = FString::Printf(
      TEXT("{\"schema\":%s,\"status\":\"captured\",\"snapshot_id\":%s,"
           "\"action\":%s,\"actor_binding_id\":%s,\"target_binding_id\":%s,"
           "\"engine_time\":%.17g,\"state_digest\":%s}"),
      *Quote(Envelope.Operation->ResponseSchema), *Quote(Output.SnapshotId),
      *Quote(ActionName), *Quote(ActorBinding),
      TargetBinding.IsSet() ? *Quote(TargetBinding.GetValue()) : TEXT("null"),
      Output.EngineTimeSec, *Quote(Output.StateDigest));
  return true;
}

bool UVistaAnimationContentApiSubsystem::FImplementation::HandleStart(
    const FEnvelope &Envelope, FString &OutPayload) {
  const FValue &Request = *Envelope.Request;
  FString PreflightId, RunId, EventId, BridgeActionId, ActorBinding;
  TOptional<FString> TargetBinding;
  if (!ValidateLifecycleIdentity(
          Request, *Envelope.Operation,
          {TEXT("bridge_action_id"), TEXT("actor_binding_id"),
           TEXT("target_binding_id"), TEXT("parameters")},
          PreflightId, RunId, EventId, OutPayload) ||
      !ReadRequiredString(Request, TEXT("bridge_action_id"), BridgeActionId) ||
      !ReadRequiredString(Request, TEXT("actor_binding_id"), ActorBinding) ||
      !IsSafeId(ActorBinding) ||
      !ReadRequiredNullableString(Request, TEXT("target_binding_id"),
                                  TargetBinding) ||
      (TargetBinding.IsSet() && !IsSafeId(TargetBinding.GetValue()))) {
    if (OutPayload.IsEmpty())
      OutPayload = MakeError(TEXT("ANIMATION_START_REQUEST_INVALID"));
    return false;
  }
  const FActionContract *Action = FindActionByBridgeId(BridgeActionId);
  const FValue *Parameters = Field(Request, TEXT("parameters"));
  FVistaAnimationActionParameters TypedParameters;
  if (!Action || !Parameters || !ValidateTargetPolicy(*Action, TargetBinding)) {
    OutPayload = MakeError(TEXT("ANIMATION_START_REQUEST_INVALID"));
    return false;
  }
  auto NumberInRange = [Parameters](const TCHAR *Name, double Min, double Max,
                                    double &Out) {
    return ReadRequiredNumber(*Parameters, Name, Out) && Out >= Min &&
           Out <= Max;
  };
  auto PositiveDuration = [Parameters](double &Out) {
    return ReadRequiredNumber(*Parameters, TEXT("duration_sec"), Out) &&
           Out > 0.0 && Out <= 60.0;
  };
  FString Error;
  switch (Action->Action) {
  case EVistaAnimationAction::LookAt:
  case EVistaAnimationAction::Pause:
    if (!ExactKeys(*Parameters, {TEXT("duration_sec")}, Error) ||
        !PositiveDuration(TypedParameters.DurationSec))
      goto InvalidParameters;
    break;
  case EVistaAnimationAction::PickUp:
  case EVistaAnimationAction::Brace:
    if (!ExactKeys(*Parameters, {TEXT("hand"), TEXT("duration_sec")}, Error) ||
        !ReadRequiredString(*Parameters, TEXT("hand"), TypedParameters.Hand) ||
        (TypedParameters.Hand != TEXT("left") &&
         TypedParameters.Hand != TEXT("right") &&
         TypedParameters.Hand != TEXT("both")) ||
        !PositiveDuration(TypedParameters.DurationSec))
      goto InvalidParameters;
    break;
  case EVistaAnimationAction::Drag:
    if (!ExactKeys(*Parameters,
                   {TEXT("hand"), TEXT("distance_cm"), TEXT("duration_sec")},
                   Error) ||
        !ReadRequiredString(*Parameters, TEXT("hand"), TypedParameters.Hand) ||
        (TypedParameters.Hand != TEXT("left") &&
         TypedParameters.Hand != TEXT("right") &&
         TypedParameters.Hand != TEXT("both")) ||
        !NumberInRange(TEXT("distance_cm"), 1.0, 500.0,
                       TypedParameters.DistanceCm) ||
        !PositiveDuration(TypedParameters.DurationSec))
      goto InvalidParameters;
    break;
  case EVistaAnimationAction::LiftFoot:
    if (!ExactKeys(*Parameters,
                   {TEXT("foot"), TEXT("height_cm"), TEXT("duration_sec")},
                   Error) ||
        !ReadRequiredString(*Parameters, TEXT("foot"), TypedParameters.Foot) ||
        (TypedParameters.Foot != TEXT("left") &&
         TypedParameters.Foot != TEXT("right")) ||
        !NumberInRange(TEXT("height_cm"), 1.0, 150.0,
                       TypedParameters.HeightCm) ||
        !PositiveDuration(TypedParameters.DurationSec))
      goto InvalidParameters;
    break;
  case EVistaAnimationAction::Fall:
  case EVistaAnimationAction::Recover:
    if (!ExactKeys(*Parameters, {TEXT("direction")}, Error) ||
        !ReadRequiredString(*Parameters, TEXT("direction"),
                            TypedParameters.Direction) ||
        (TypedParameters.Direction != TEXT("forward") &&
         TypedParameters.Direction != TEXT("backward") &&
         TypedParameters.Direction != TEXT("left") &&
         TypedParameters.Direction != TEXT("right")))
      goto InvalidParameters;
    break;
  }

  {
    const FString Key = EventKey(PreflightId, RunId, EventId);
    TSharedPtr<IVistaAnimationContentDriver, ESPMode::ThreadSafe> LocalDriver;
    {
      FScopeLock Guard(&Mutex);
      FSnapshotState *Snapshot = Snapshots.Find(Key);
      if (!BindPreflightLocked(PreflightId) || !Snapshot ||
          Snapshot->bStartAttempted || Snapshot->Action != Action->Action ||
          Snapshot->ActorBindingId != ActorBinding ||
          Snapshot->TargetBindingId != TargetBinding) {
        OutPayload = MakeError(TEXT("ANIMATION_START_SNAPSHOT_MISMATCH"));
        return false;
      }
      Snapshot->bStartAttempted = true;
      LocalDriver = Driver;
    }
    if (!BeginMutation(Envelope, OutPayload))
      return false;
    FVistaAnimationStartOutput Output;
    FString DriverError;
    if (!LocalDriver ||
        !LocalDriver->Start(Action->Action, ActorBinding, TargetBinding,
                            TypedParameters, Output, DriverError)) {
      OutPayload = MakeError(IsSafeErrorCode(DriverError)
                                 ? DriverError
                                 : TEXT("ANIMATION_START_DRIVER_FAILED"));
      return false;
    }
    if (!IsOpaqueId(Output.ActionHandle) ||
        !FMath::IsFinite(Output.EngineTimeSec) || Output.EngineTimeSec < 0 ||
        Output.EngineTimeSec > MaxEngineTimeSec) {
      OutPayload = MakeError(TEXT("ANIMATION_START_DRIVER_PROTOCOL_INVALID"));
      return false;
    }
    {
      FScopeLock Guard(&Mutex);
      if (Handles.Contains(Output.ActionHandle)) {
        OutPayload = MakeError(TEXT("ANIMATION_ACTION_HANDLE_REUSED"));
        return false;
      }
      FSnapshotState &Snapshot = Snapshots.FindChecked(Key);
      Snapshot.ActionHandle = Output.ActionHandle;
      Handles.Add(Output.ActionHandle,
                  {Key, Action->Action, EHandleState::Started, false, false});
    }
    OutPayload = FString::Printf(
        TEXT("{\"schema\":%s,\"status\":\"started\",\"action_handle\":%s,"
             "\"bridge_action_id\":%s,\"actor_binding_id\":%s,\"target_binding_"
             "id\":%s,\"engine_time\":%.17g}"),
        *Quote(Envelope.Operation->ResponseSchema), *Quote(Output.ActionHandle),
        *Quote(BridgeActionId), *Quote(ActorBinding),
        TargetBinding.IsSet() ? *Quote(TargetBinding.GetValue()) : TEXT("null"),
        Output.EngineTimeSec);
    return true;
  }

InvalidParameters:
  OutPayload = MakeError(TEXT("ANIMATION_ACTION_PARAMETERS_INVALID"));
  return false;
}

bool UVistaAnimationContentApiSubsystem::FImplementation::HandleWait(
    const FEnvelope &Envelope, FString &OutPayload) {
  const FValue &Request = *Envelope.Request;
  FString PreflightId, RunId, EventId, ActionHandle;
  if (!ValidateLifecycleIdentity(Request, *Envelope.Operation,
                                 {TEXT("action_handle")}, PreflightId, RunId,
                                 EventId, OutPayload) ||
      !ReadRequiredString(Request, TEXT("action_handle"), ActionHandle) ||
      !IsOpaqueId(ActionHandle)) {
    if (OutPayload.IsEmpty())
      OutPayload = MakeError(TEXT("ANIMATION_WAIT_REQUEST_INVALID"));
    return false;
  }
  FString CompletionSignal;
  TSharedPtr<IVistaAnimationContentDriver, ESPMode::ThreadSafe> LocalDriver;
  {
    FScopeLock Guard(&Mutex);
    FHandleState *Handle = Handles.Find(ActionHandle);
    if (!BindPreflightLocked(PreflightId) || !Handle ||
        Handle->EventKey != EventKey(PreflightId, RunId, EventId) ||
        Handle->State != EHandleState::Started) {
      OutPayload = MakeError(TEXT("ANIMATION_ACTION_HANDLE_INVALID"));
      return false;
    }
    const FVistaAnimationTrustedAction *Action = TrustedAction(Handle->Action);
    if (!Action) {
      OutPayload = MakeError(TEXT("ANIMATION_ACTION_UNVERIFIED"));
      return false;
    }
    CompletionSignal = Action->CompletionSignal;
    LocalDriver = Driver;
  }
  FVistaAnimationWaitOutput Output;
  FString DriverError;
  if (!LocalDriver || !LocalDriver->Wait(ActionHandle, Output, DriverError)) {
    OutPayload = MakeError(IsSafeErrorCode(DriverError)
                               ? DriverError
                               : TEXT("ANIMATION_WAIT_DRIVER_FAILED"),
                           true);
    return false;
  }
  if (!Output.bCompleted ||
      Output.ObservedCompletionSignal != CompletionSignal) {
    OutPayload = MakeError(TEXT("ANIMATION_COMPLETION_SIGNAL_MISMATCH"));
    return false;
  }
  Output.EvidenceIds.Sort();
  if (!IsOpaqueId(Output.CompletionEvidenceId) ||
      !IsLowerHex(Output.CompletionEvidenceSha256, 64) ||
      !FMath::IsFinite(Output.EngineTimeSec) || Output.EngineTimeSec < 0 ||
      Output.EngineTimeSec > MaxEngineTimeSec || Output.EvidenceIds.IsEmpty() ||
      Output.EvidenceIds.Num() > 32) {
    OutPayload = MakeError(TEXT("ANIMATION_WAIT_DRIVER_PROTOCOL_INVALID"));
    return false;
  }
  for (int32 Index = 0; Index < Output.EvidenceIds.Num(); ++Index) {
    if (!IsOpaqueId(Output.EvidenceIds[Index]) ||
        (Index > 0 &&
         Output.EvidenceIds[Index] == Output.EvidenceIds[Index - 1])) {
      OutPayload = MakeError(TEXT("ANIMATION_WAIT_DRIVER_PROTOCOL_INVALID"));
      return false;
    }
  }
  if (!Output.EvidenceIds.Contains(Output.CompletionEvidenceId)) {
    OutPayload = MakeError(TEXT("ANIMATION_WAIT_DRIVER_PROTOCOL_INVALID"));
    return false;
  }
  {
    FScopeLock Guard(&Mutex);
    FHandleState *Handle = Handles.Find(ActionHandle);
    if (!Handle || Handle->State != EHandleState::Started) {
      OutPayload = MakeError(TEXT("ANIMATION_ACTION_STATE_CONFLICT"));
      return false;
    }
    Handle->State = EHandleState::Completed;
  }
  OutPayload = FString::Printf(
      TEXT("{\"schema\":%s,\"status\":\"completed\",\"action_handle\":%s,"
           "\"completion_signal\":%s,\"engine_time\":%.17g,\"evidence_ids\":%"
           "s}"),
      *Quote(Envelope.Operation->ResponseSchema), *Quote(ActionHandle),
      *Quote(Output.ObservedCompletionSignal), Output.EngineTimeSec,
      *JsonStringArray(Output.EvidenceIds));
  return true;
}

bool UVistaAnimationContentApiSubsystem::FImplementation::HandleStop(
    const FEnvelope &Envelope, FString &OutPayload) {
  const FValue &Request = *Envelope.Request;
  FString PreflightId, RunId, EventId, ActionHandle, Reason;
  if (!ValidateLifecycleIdentity(Request, *Envelope.Operation,
                                 {TEXT("action_handle"), TEXT("reason")},
                                 PreflightId, RunId, EventId, OutPayload) ||
      !ReadRequiredString(Request, TEXT("action_handle"), ActionHandle) ||
      !IsOpaqueId(ActionHandle) ||
      !ReadRequiredString(Request, TEXT("reason"), Reason) ||
      (Reason != TEXT("timeout") && Reason != TEXT("cancel") &&
       Reason != TEXT("cleanup_failed") &&
       Reason != TEXT("cleanup_cancelled") &&
       Reason != TEXT("cleanup_timed_out"))) {
    if (OutPayload.IsEmpty())
      OutPayload = MakeError(TEXT("ANIMATION_STOP_REQUEST_INVALID"));
    return false;
  }
  TSharedPtr<IVistaAnimationContentDriver, ESPMode::ThreadSafe> LocalDriver;
  {
    FScopeLock Guard(&Mutex);
    FHandleState *Handle = Handles.Find(ActionHandle);
    if (!BindPreflightLocked(PreflightId) || !Handle ||
        Handle->EventKey != EventKey(PreflightId, RunId, EventId) ||
        Handle->bStopAttempted ||
        (Handle->State != EHandleState::Started &&
         Handle->State != EHandleState::Completed)) {
      OutPayload = MakeError(TEXT("ANIMATION_STOP_STATE_CONFLICT"));
      return false;
    }
    Handle->bStopAttempted = true;
    LocalDriver = Driver;
  }
  if (!BeginMutation(Envelope, OutPayload))
    return false;
  FVistaAnimationStopOutput Output;
  FString DriverError;
  if (!LocalDriver ||
      !LocalDriver->Stop(ActionHandle, Reason, Output, DriverError)) {
    OutPayload = MakeError(IsSafeErrorCode(DriverError)
                               ? DriverError
                               : TEXT("ANIMATION_STOP_DRIVER_FAILED"));
    return false;
  }
  if (!FMath::IsFinite(Output.EngineTimeSec) || Output.EngineTimeSec < 0 ||
      Output.EngineTimeSec > MaxEngineTimeSec) {
    OutPayload = MakeError(TEXT("ANIMATION_STOP_DRIVER_PROTOCOL_INVALID"));
    return false;
  }
  {
    FScopeLock Guard(&Mutex);
    Handles.FindChecked(ActionHandle).State = EHandleState::Stopped;
  }
  OutPayload = FString::Printf(TEXT("{\"schema\":%s,\"status\":\"%s\",\"action_"
                                    "handle\":%s,\"engine_time\":%.17g}"),
                               *Quote(Envelope.Operation->ResponseSchema),
                               Output.bAlreadyStopped ? TEXT("already_stopped")
                                                      : TEXT("stopped"),
                               *Quote(ActionHandle), Output.EngineTimeSec);
  return true;
}

bool UVistaAnimationContentApiSubsystem::FImplementation::HandleRelease(
    const FEnvelope &Envelope, FString &OutPayload) {
  const FValue &Request = *Envelope.Request;
  FString PreflightId, RunId, EventId, ActionHandle;
  if (!ValidateLifecycleIdentity(Request, *Envelope.Operation,
                                 {TEXT("action_handle")}, PreflightId, RunId,
                                 EventId, OutPayload) ||
      !ReadRequiredString(Request, TEXT("action_handle"), ActionHandle) ||
      !IsOpaqueId(ActionHandle)) {
    if (OutPayload.IsEmpty())
      OutPayload = MakeError(TEXT("ANIMATION_RELEASE_REQUEST_INVALID"));
    return false;
  }
  TSharedPtr<IVistaAnimationContentDriver, ESPMode::ThreadSafe> LocalDriver;
  bool bCompletedBeforeRelease = false;
  {
    FScopeLock Guard(&Mutex);
    FHandleState *Handle = Handles.Find(ActionHandle);
    if (!BindPreflightLocked(PreflightId) || !Handle ||
        Handle->EventKey != EventKey(PreflightId, RunId, EventId) ||
        Handle->bReleaseAttempted ||
        (Handle->State != EHandleState::Completed &&
         Handle->State != EHandleState::Stopped)) {
      OutPayload = MakeError(TEXT("ANIMATION_RELEASE_STATE_CONFLICT"));
      return false;
    }
    bCompletedBeforeRelease = Handle->State == EHandleState::Completed;
    Handle->bReleaseAttempted = true;
    LocalDriver = Driver;
  }
  if (!BeginMutation(Envelope, OutPayload))
    return false;
  FVistaAnimationReleaseOutput Output;
  FString DriverError;
  if (!LocalDriver ||
      !LocalDriver->Release(ActionHandle, Output, DriverError)) {
    OutPayload = MakeError(IsSafeErrorCode(DriverError)
                               ? DriverError
                               : TEXT("ANIMATION_RELEASE_DRIVER_FAILED"));
    return false;
  }
  {
    FScopeLock Guard(&Mutex);
    FHandleState &Handle = Handles.FindChecked(ActionHandle);
    Handle.State = EHandleState::Released;
    if (bCompletedBeforeRelease)
      Snapshots.Remove(Handle.EventKey);
  }
  OutPayload = FString::Printf(
      TEXT("{\"schema\":%s,\"status\":\"%s\",\"action_handle\":%s}"),
      *Quote(Envelope.Operation->ResponseSchema),
      Output.bAlreadyReleased ? TEXT("already_released") : TEXT("released"),
      *Quote(ActionHandle));
  return true;
}

bool UVistaAnimationContentApiSubsystem::FImplementation::HandleRestore(
    const FEnvelope &Envelope, FString &OutPayload) {
  const FValue &Request = *Envelope.Request;
  FString PreflightId, RunId, EventId, SnapshotId, StateDigest;
  if (!ValidateLifecycleIdentity(Request, *Envelope.Operation,
                                 {TEXT("snapshot_id"), TEXT("state_digest")},
                                 PreflightId, RunId, EventId, OutPayload) ||
      !ReadRequiredString(Request, TEXT("snapshot_id"), SnapshotId) ||
      !IsOpaqueId(SnapshotId) ||
      !ReadRequiredString(Request, TEXT("state_digest"), StateDigest) ||
      !IsLowerHex(StateDigest, 64)) {
    if (OutPayload.IsEmpty())
      OutPayload = MakeError(TEXT("ANIMATION_RESTORE_REQUEST_INVALID"));
    return false;
  }
  const FString Key = EventKey(PreflightId, RunId, EventId);
  TSharedPtr<IVistaAnimationContentDriver, ESPMode::ThreadSafe> LocalDriver;
  {
    FScopeLock Guard(&Mutex);
    FSnapshotState *Snapshot = Snapshots.Find(Key);
    if (!BindPreflightLocked(PreflightId) || !Snapshot ||
        Snapshot->SnapshotId != SnapshotId ||
        Snapshot->StateDigest != StateDigest || Snapshot->bRestoreAttempted) {
      OutPayload = MakeError(TEXT("ANIMATION_RESTORE_SNAPSHOT_MISMATCH"));
      return false;
    }
    if (!Snapshot->ActionHandle.IsEmpty()) {
      const FHandleState *Handle = Handles.Find(Snapshot->ActionHandle);
      if (Handle && Handle->State != EHandleState::Released) {
        OutPayload = MakeError(TEXT("ANIMATION_RELEASE_REQUIRED"));
        return false;
      }
    }
    Snapshot->bRestoreAttempted = true;
    LocalDriver = Driver;
  }
  if (!BeginMutation(Envelope, OutPayload))
    return false;
  FVistaAnimationRestoreOutput Output;
  FString DriverError;
  if (!LocalDriver ||
      !LocalDriver->Restore(SnapshotId, StateDigest, Output, DriverError)) {
    OutPayload = MakeError(IsSafeErrorCode(DriverError)
                               ? DriverError
                               : TEXT("ANIMATION_RESTORE_DRIVER_FAILED"));
    return false;
  }
  if (!FMath::IsFinite(Output.EngineTimeSec) || Output.EngineTimeSec < 0 ||
      Output.EngineTimeSec > MaxEngineTimeSec) {
    OutPayload = MakeError(TEXT("ANIMATION_RESTORE_DRIVER_PROTOCOL_INVALID"));
    return false;
  }
  {
    FScopeLock Guard(&Mutex);
    FSnapshotState &Snapshot = Snapshots.FindChecked(Key);
    if (!Snapshot.ActionHandle.IsEmpty() &&
        Handles.Contains(Snapshot.ActionHandle))
      Handles.FindChecked(Snapshot.ActionHandle).State = EHandleState::Restored;
    Snapshots.Remove(Key);
  }
  OutPayload = FString::Printf(
      TEXT("{\"schema\":%s,\"status\":\"restored\",\"snapshot_id\":%s,\"state_"
           "digest\":%s,\"engine_time\":%.17g}"),
      *Quote(Envelope.Operation->ResponseSchema), *Quote(SnapshotId),
      *Quote(StateDigest), Output.EngineTimeSec);
  return true;
}

void UVistaAnimationContentApiSubsystem::Initialize(
    FSubsystemCollectionBase &Collection) {
  Super::Initialize(Collection);
  Implementation.Reset(new FImplementation());
}

void UVistaAnimationContentApiSubsystem::Deinitialize() {
  if (Implementation)
    Implementation->Reset();
  Implementation.Reset();
  Super::Deinitialize();
}

bool UVistaAnimationContentApiSubsystem::ConfigureTrustedRuntime(
    const FVistaAnimationTrustedRuntimeConfig &Config,
    TSharedRef<IVistaAnimationContentDriver, ESPMode::ThreadSafe> Driver,
    FString &OutSafeErrorCode) {
  if (!Implementation) {
    OutSafeErrorCode = TEXT("ANIMATION_SUBSYSTEM_UNAVAILABLE");
    return false;
  }
  return Implementation->Configure(Config, Driver, OutSafeErrorCode);
}

EVistaAnimationFixedDispatchResult
UVistaAnimationContentApiSubsystem::DispatchFixedJsonCommand(
    const FString &CommandType, const FString &RequestJson,
    FString &OutResponseJson) {
  if (!Implementation) {
    OutResponseJson = MakeError(TEXT("ANIMATION_SUBSYSTEM_UNAVAILABLE"));
    return EVistaAnimationFixedDispatchResult::Unavailable;
  }
  if (CommandType == TEXT("vista_animation_capabilities")) {
    HandleCapabilityProbeJson(RequestJson, OutResponseJson);
    return EVistaAnimationFixedDispatchResult::Handled;
  }
  if (CommandType == TEXT("vista_animation_content_api")) {
    HandleContentRequestJson(RequestJson, OutResponseJson);
    return EVistaAnimationFixedDispatchResult::Handled;
  }
  if (CommandType == TEXT("vista_animation_engine_time")) {
    HandleEngineTimeJson(RequestJson, OutResponseJson);
    return EVistaAnimationFixedDispatchResult::Handled;
  }
  if (CommandType == TEXT("vista_animation_evidence_capture")) {
    HandleEvidenceCaptureJson(RequestJson, OutResponseJson);
    return EVistaAnimationFixedDispatchResult::Handled;
  }
  OutResponseJson = MakeError(TEXT("ANIMATION_COMMAND_TYPE_FORBIDDEN"));
  return EVistaAnimationFixedDispatchResult::RejectedUnknownCommand;
}

bool UVistaAnimationContentApiSubsystem::HandleCapabilityProbeJson(
    const FString &RequestJson, FString &OutResponseJson) {
  if (!Implementation) {
    OutResponseJson = MakeError(TEXT("ANIMATION_SUBSYSTEM_UNAVAILABLE"));
    return false;
  }
  return Implementation->Capability(RequestJson, OutResponseJson);
}

bool UVistaAnimationContentApiSubsystem::HandleContentRequestJson(
    const FString &RequestJson, FString &OutResponseJson) {
  if (!Implementation) {
    OutResponseJson = MakeError(TEXT("ANIMATION_SUBSYSTEM_UNAVAILABLE"));
    return false;
  }
  return Implementation->Content(RequestJson, OutResponseJson);
}

bool UVistaAnimationContentApiSubsystem::HandleEngineTimeJson(
    const FString &RequestJson, FString &OutResponseJson) {
  if (!Implementation) {
    OutResponseJson = MakeError(TEXT("ANIMATION_SUBSYSTEM_UNAVAILABLE"));
    return false;
  }
  return Implementation->EngineTime(RequestJson, OutResponseJson);
}

bool UVistaAnimationContentApiSubsystem::HandleEvidenceCaptureJson(
    const FString &RequestJson, FString &OutResponseJson) {
  if (!Implementation) {
    OutResponseJson = MakeError(TEXT("ANIMATION_SUBSYSTEM_UNAVAILABLE"));
    return false;
  }
  return Implementation->EvidenceCapture(RequestJson, OutResponseJson);
}

#undef MakeError
