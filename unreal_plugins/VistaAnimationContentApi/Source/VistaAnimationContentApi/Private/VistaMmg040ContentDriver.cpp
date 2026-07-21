#include "VistaMmg040ContentDriver.h"

#include "VistaAnimationStrictJson.h"

#include "Misc/Guid.h"
#include "Misc/ScopeLock.h"

namespace {
using VistaAnimation::StrictJson::Quote;
using VistaAnimation::StrictJson::Sha256HexUtf8;

const TCHAR *ProfileId = TEXT("vista_mmg040");
const TCHAR *ProfileRevision = TEXT("mmg040_project_content_r2");
const TCHAR *ContentBindingSchema = TEXT("vista-animation-content-binding/v1");
const TCHAR *ReceiptSchema =
    TEXT("vista-animation-content-inspection-receipt/v1");
const TCHAR *LegacyHandTraceCompletionSignal = TEXT("EndHandTrace");
const TCHAR *ProjectName = TEXT("gym_citynav");
const TCHAR *EngineVersion = TEXT("5.3.2");
const TCHAR *VerificationStatus = TEXT("verified");
const TCHAR *VerificationMethod = TEXT("ue53_disposable_live_inspection_v1");
constexpr double MaxEngineTimeSec = 315360000.0;
constexpr uint64 MaxActionHandlesPerDriver = 1000000;

const TArray<FString> &RequiredReceiptChecks() {
  static const TArray<FString> Values = {
      TEXT("pawn_spawnable"),        TEXT("generated_class_matches"),
      TEXT("skeletal_mesh_matches"), TEXT("anim_blueprint_matches"),
      TEXT("no_redirectors"),        TEXT("dependency_closure"),
      TEXT("disposable_pie"),        TEXT("scene_zero_diff")};
  return Values;
}

bool IsLowerHex(const FString &Value, int32 Length) {
  if (Value.Len() != Length)
    return false;
  for (const TCHAR Character : Value)
    if (!((Character >= TEXT('0') && Character <= TEXT('9')) ||
          (Character >= TEXT('a') && Character <= TEXT('f'))))
      return false;
  return true;
}

bool IsOpaqueId(const FString &Value) {
  auto IsAsciiAlnum = [](TCHAR Character) {
    return (Character >= TEXT('A') && Character <= TEXT('Z')) ||
           (Character >= TEXT('a') && Character <= TEXT('z')) ||
           (Character >= TEXT('0') && Character <= TEXT('9'));
  };
  if (Value.IsEmpty() || Value.Len() > 160 || !IsAsciiAlnum(Value[0]))
    return false;
  for (const TCHAR Character : Value)
    if (!(IsAsciiAlnum(Character) || Character == TEXT('.') ||
          Character == TEXT('_') || Character == TEXT(':') ||
          Character == TEXT('@') || Character == TEXT('-')))
      return false;
  return true;
}

bool IsSafeErrorCode(const FString &Value) {
  if (!Value.StartsWith(TEXT("ANIMATION_")) || Value.Len() > 120)
    return false;
  for (const TCHAR Character : Value)
    if (!((Character >= TEXT('A') && Character <= TEXT('Z')) ||
          (Character >= TEXT('0') && Character <= TEXT('9')) ||
          Character == TEXT('_')))
      return false;
  return true;
}

bool IsUtcTimestamp(const FString &Value) {
  if (Value.Len() < 20 || Value.Len() > 24 || !Value.EndsWith(TEXT("Z")))
    return false;
  FDateTime Parsed;
  return FDateTime::ParseIso8601(*Value, Parsed);
}

const TCHAR *ActionName(EVistaAnimationAction Action) {
  switch (Action) {
  case EVistaAnimationAction::LookAt:
    return TEXT("look_at");
  case EVistaAnimationAction::PickUp:
    return TEXT("pick_up");
  case EVistaAnimationAction::Brace:
    return TEXT("brace");
  case EVistaAnimationAction::Drag:
    return TEXT("drag");
  case EVistaAnimationAction::LiftFoot:
    return TEXT("lift_foot");
  case EVistaAnimationAction::Pause:
    return TEXT("pause");
  case EVistaAnimationAction::Fall:
    return TEXT("fall");
  case EVistaAnimationAction::Recover:
    return TEXT("recover");
  }
  return TEXT("invalid");
}

FString ActionTargetKey(EVistaAnimationAction Action,
                        const FString &TargetBindingId) {
  return FString(ActionName(Action)) + TEXT("|") + TargetBindingId;
}

const FVistaMmg040PinnedAssetContract *
FindAsset(EVistaMmg040PinnedAsset Asset) {
  for (const FVistaMmg040PinnedAssetContract &Candidate :
       FVistaMmg040ContentDriver::PinnedAssets())
    if (Candidate.Asset == Asset)
      return &Candidate;
  return nullptr;
}

const FVistaMmg040PinnedActionContract *
FindAction(EVistaAnimationAction Action) {
  for (const FVistaMmg040PinnedActionContract &Candidate :
       FVistaMmg040ContentDriver::PinnedActions())
    if (Candidate.Action == Action)
      return &Candidate;
  return nullptr;
}

template <typename T>
bool SameOptional(const TOptional<T> &Left, const TOptional<T> &Right) {
  return Left.IsSet() == Right.IsSet() &&
         (!Left.IsSet() || Left.GetValue() == Right.GetValue());
}

bool SameStringSet(TArray<FString> Left, TArray<FString> Right) {
  Left.Sort();
  Right.Sort();
  if (Left.Num() != Right.Num())
    return false;
  for (int32 Index = 0; Index < Left.Num(); ++Index)
    if (Left[Index] != Right[Index] ||
        (Index > 0 && Left[Index] == Left[Index - 1]))
      return false;
  return true;
}

bool SameParameters(const FVistaMmg040ExactParameterContract &Left,
                    const FVistaMmg040ExactParameterContract &Right) {
  return SameOptional(Left.DurationSec, Right.DurationSec) &&
         SameOptional(Left.DistanceCm, Right.DistanceCm) &&
         SameOptional(Left.HeightCm, Right.HeightCm) &&
         SameOptional(Left.Hand, Right.Hand) &&
         SameOptional(Left.Foot, Right.Foot) &&
         SameOptional(Left.Direction, Right.Direction);
}

bool MatchesRuntimeParameters(
    const FVistaAnimationActionParameters &Observed,
    const FVistaMmg040ExactParameterContract &Expected) {
  auto SameNumber = [](double Value, const TOptional<int32> &Pin) {
    return Pin.IsSet() ? Value == static_cast<double>(Pin.GetValue())
                       : Value == 0.0;
  };
  auto SameText = [](const FString &Value, const TOptional<FString> &Pin) {
    return Pin.IsSet() ? Value == Pin.GetValue() : Value.IsEmpty();
  };
  return SameNumber(Observed.DurationSec, Expected.DurationSec) &&
         SameNumber(Observed.DistanceCm, Expected.DistanceCm) &&
         SameNumber(Observed.HeightCm, Expected.HeightCm) &&
         SameText(Observed.Hand, Expected.Hand) &&
         SameText(Observed.Foot, Expected.Foot) &&
         SameText(Observed.Direction, Expected.Direction);
}

FString OptionalIntJson(const TOptional<int32> &Value) {
  return Value.IsSet() ? FString::FromInt(Value.GetValue()) : TEXT("null");
}

FString OptionalStringJson(const TOptional<FString> &Value) {
  return Value.IsSet() ? Quote(Value.GetValue()) : TEXT("null");
}

FString ParametersJson(const FVistaMmg040ExactParameterContract &Value) {
  return FString::Printf(
      TEXT("{\"direction\":%s,\"distance_cm\":%s,\"duration_sec\":%s,"
           "\"foot\":%s,\"hand\":%s,\"height_cm\":%s}"),
      *OptionalStringJson(Value.Direction), *OptionalIntJson(Value.DistanceCm),
      *OptionalIntJson(Value.DurationSec), *OptionalStringJson(Value.Foot),
      *OptionalStringJson(Value.Hand), *OptionalIntJson(Value.HeightCm));
}

FString SortedStringArrayJson(TArray<FString> Values) {
  Values.Sort();
  TArray<FString> Encoded;
  for (const FString &Value : Values)
    Encoded.Add(Quote(Value));
  return TEXT("[") + FString::Join(Encoded, TEXT(",")) + TEXT("]");
}

bool ContainsAll(const TArray<FString> &Available,
                 const TArray<FString> &Required) {
  TSet<FString> Values;
  for (const FString &Value : Available)
    Values.Add(Value);
  for (const FString &Value : Required)
    if (!Values.Contains(Value))
      return false;
  return true;
}

const FVistaMmg040VerifiedAssetReceipt *
FindAssetReceipt(const FVistaMmg040VerifiedProfileReceipt &Receipt,
                 EVistaMmg040PinnedAsset Asset) {
  for (const FVistaMmg040VerifiedAssetReceipt &Candidate : Receipt.Assets)
    if (Candidate.Asset == Asset)
      return &Candidate;
  return nullptr;
}

const FVistaMmg040VerifiedActionReceipt *
FindActionReceipt(const FVistaMmg040VerifiedProfileReceipt &Receipt,
                  EVistaAnimationAction Action) {
  for (const FVistaMmg040VerifiedActionReceipt &Candidate : Receipt.Actions)
    if (Candidate.Action == Action)
      return &Candidate;
  return nullptr;
}

bool ExpectedBehaviorFlag(EVistaAnimationAction Action, const TCHAR *FlagName) {
  if (FCString::Strcmp(FlagName, TEXT("ik")) == 0)
    return Action == EVistaAnimationAction::PickUp ||
           Action == EVistaAnimationAction::Brace ||
           Action == EVistaAnimationAction::Drag ||
           Action == EVistaAnimationAction::LiftFoot;
  if (FCString::Strcmp(FlagName, TEXT("object_attachment")) == 0)
    return Action == EVistaAnimationAction::PickUp;
  if (FCString::Strcmp(FlagName, TEXT("root_motion")) == 0)
    return Action == EVistaAnimationAction::Drag ||
           Action == EVistaAnimationAction::Fall ||
           Action == EVistaAnimationAction::Recover;
  if (FCString::Strcmp(FlagName, TEXT("collision")) == 0)
    return Action == EVistaAnimationAction::Fall;
  if (FCString::Strcmp(FlagName, TEXT("recovery")) == 0)
    return Action == EVistaAnimationAction::Recover;
  return false;
}

FString BuildContentDigest(const FVistaMmg040VerifiedProfileReceipt &Receipt) {
  struct FCanonicalEntry {
    FString Name;
    FString Json;
  };
  TArray<FCanonicalEntry> Assets;
  for (const FVistaMmg040VerifiedAssetReceipt &Asset : Receipt.Assets) {
    const FVistaMmg040PinnedAssetContract *Pin = FindAsset(Asset.Asset);
    if (!Pin)
      return FString();
    Assets.Add(
        {Pin->AssetId,
         FString::Printf(TEXT("{\"asset_id\":%s,\"package_sha256\":%s}"),
                         *Quote(Pin->AssetId), *Quote(Asset.PackageSha256))});
  }
  Assets.Sort([](const FCanonicalEntry &Left, const FCanonicalEntry &Right) {
    return Left.Name < Right.Name;
  });
  TArray<FString> AssetJson;
  for (const FCanonicalEntry &Entry : Assets)
    AssetJson.Add(Entry.Json);

  TArray<FCanonicalEntry> Actions;
  for (const FVistaMmg040VerifiedActionReceipt &Action : Receipt.Actions) {
    const FVistaMmg040PinnedActionContract *Pin = FindAction(Action.Action);
    if (!Pin)
      return FString();
    Actions.Add(
        {Pin->ActionName,
         FString::Printf(
             TEXT("{\"action\":%s,\"behavior_evidence_sha256\":%s,"
                  "\"observed_live_checks\":%s,\"verified_parameters\":%s}"),
             *Quote(Pin->ActionName), *Quote(Action.BehaviorEvidenceSha256),
             *SortedStringArrayJson(Action.ObservedLiveChecks),
             *ParametersJson(Action.VerifiedParameters))});
  }
  Actions.Sort([](const FCanonicalEntry &Left, const FCanonicalEntry &Right) {
    return Left.Name < Right.Name;
  });
  TArray<FString> ActionJson;
  for (const FCanonicalEntry &Entry : Actions)
    ActionJson.Add(Entry.Json);

  const FString Canonical = FString::Printf(
      TEXT("{\"actions\":[%s],\"assets\":[%s],\"content_revision\":%s,"
           "\"project_descriptor_sha256\":%s,\"project_revision\":%s,"
           "\"schema\":%s,\"source_contract_sha256\":%s}"),
      *FString::Join(ActionJson, TEXT(",")),
      *FString::Join(AssetJson, TEXT(",")), *Quote(Receipt.ContentRevision),
      *Quote(Receipt.ProjectDescriptorSha256), *Quote(Receipt.ProjectRevision),
      *Quote(ContentBindingSchema), *Quote(Receipt.SourceContractSha256));
  return Sha256HexUtf8(Canonical);
}

bool ValidateInspection(const FVistaMmg040AssetInspection &Observed,
                        const FVistaMmg040PinnedAssetContract &Pin,
                        const FVistaMmg040VerifiedAssetReceipt &Receipt,
                        FString &OutError) {
  const bool bExpectedRootMotion =
      Pin.RootMotionPolicy == EVistaMmg040RootMotionPolicy::Required;
  if (!Observed.bLoaded) {
    OutError = TEXT("ANIMATION_MMG040_PINNED_ASSET_MISSING");
    return false;
  }
  if (Observed.Asset != Pin.Asset || Observed.ObjectPath != Pin.ObjectPath ||
      Observed.ObservedClass != Pin.ExpectedClass ||
      !SameOptional(Observed.SkeletonAsset, Pin.SkeletonAsset)) {
    OutError = TEXT("ANIMATION_MMG040_PINNED_ASSET_MISMATCH");
    return false;
  }
  if (!IsLowerHex(Observed.PackageSha256, 64) ||
      Observed.PackageSha256 != Receipt.PackageSha256) {
    OutError = TEXT("ANIMATION_MMG040_ASSET_DIGEST_MISMATCH");
    return false;
  }
  if (!SameStringSet(Observed.NotifyNames, Pin.RequiredNotifies)) {
    OutError = TEXT("ANIMATION_MMG040_NOTIFY_MISMATCH");
    return false;
  }
  if (Observed.bRootMotionEnabled != bExpectedRootMotion) {
    OutError = TEXT("ANIMATION_MMG040_ROOT_MOTION_MISMATCH");
    return false;
  }
  return true;
}
} // namespace

const FString &FVistaMmg040ContentDriver::SourceContractSha256() {
  static const FString Value =
      TEXT("9772da93c1054a3e3dbf71d0c066e41e6f19740901cda72b258445124be4e5a4");
  return Value;
}

const TArray<FVistaMmg040PinnedAssetContract> &
FVistaMmg040ContentDriver::PinnedAssets() {
  static const TArray<FVistaMmg040PinnedAssetContract> Values = {
      {EVistaMmg040PinnedAsset::PawnClass,
       TEXT("pawn_class"),
       TEXT("/Game/VISTA/MMG040/Character/"
            "BP_MMG040Character.BP_MMG040Character_C"),
       TEXT("/Script/Engine.BlueprintGeneratedClass"),
       EVistaMmg040PinnedAsset::Skeleton,
       {},
       EVistaMmg040RootMotionPolicy::NotApplicable},
      {EVistaMmg040PinnedAsset::SkeletalMesh,
       TEXT("skeletal_mesh"),
       TEXT("/Game/VISTA/MMG040/Character/"
            "SK_MMG040Character.SK_MMG040Character"),
       TEXT("/Script/Engine.SkeletalMesh"),
       EVistaMmg040PinnedAsset::Skeleton,
       {},
       EVistaMmg040RootMotionPolicy::NotApplicable},
      {EVistaMmg040PinnedAsset::Skeleton,
       TEXT("skeleton"),
       TEXT("/Game/VISTA/MMG040/Character/"
            "SKEL_MMG040Character.SKEL_MMG040Character"),
       TEXT("/Script/Engine.Skeleton"),
       {},
       {},
       EVistaMmg040RootMotionPolicy::NotApplicable},
      {EVistaMmg040PinnedAsset::AnimBlueprint,
       TEXT("anim_blueprint"),
       TEXT("/Game/VISTA/MMG040/Character/"
            "ABP_MMG040Character.ABP_MMG040Character_C"),
       TEXT("/Script/Engine.AnimBlueprintGeneratedClass"),
       EVistaMmg040PinnedAsset::Skeleton,
       {},
       EVistaMmg040RootMotionPolicy::NotApplicable},
      {EVistaMmg040PinnedAsset::ControlRig,
       TEXT("control_rig"),
       TEXT("/Game/VISTA/MMG040/Rigs/CR_MMG040Character.CR_MMG040Character"),
       TEXT("/Script/ControlRigDeveloper.ControlRigBlueprint"),
       EVistaMmg040PinnedAsset::Skeleton,
       {},
       EVistaMmg040RootMotionPolicy::NotApplicable},
      {EVistaMmg040PinnedAsset::IkRig,
       TEXT("ik_rig"),
       TEXT("/Game/VISTA/MMG040/Rigs/IK_MMG040Character.IK_MMG040Character"),
       TEXT("/Script/IKRig.IKRigDefinition"),
       EVistaMmg040PinnedAsset::Skeleton,
       {},
       EVistaMmg040RootMotionPolicy::NotApplicable},
      {EVistaMmg040PinnedAsset::LookAtMontage,
       TEXT("look_at_montage"),
       TEXT("/Game/VISTA/MMG040/Montages/AM_MMG040_LookAt.AM_MMG040_LookAt"),
       TEXT("/Script/Engine.AnimMontage"),
       EVistaMmg040PinnedAsset::Skeleton,
       {TEXT("vista_look_at_completed")},
       EVistaMmg040RootMotionPolicy::Forbidden},
      {EVistaMmg040PinnedAsset::PickUpMontage,
       TEXT("pick_up_montage"),
       TEXT("/Game/VISTA/MMG040/Montages/AM_MMG040_PickUp.AM_MMG040_PickUp"),
       TEXT("/Script/Engine.AnimMontage"),
       EVistaMmg040PinnedAsset::Skeleton,
       {TEXT("vista_pick_up_attached")},
       EVistaMmg040RootMotionPolicy::Forbidden},
      {EVistaMmg040PinnedAsset::BraceMontage,
       TEXT("brace_montage"),
       TEXT("/Game/VISTA/MMG040/Montages/AM_MMG040_Brace.AM_MMG040_Brace"),
       TEXT("/Script/Engine.AnimMontage"),
       EVistaMmg040PinnedAsset::Skeleton,
       {TEXT("vista_brace_contact_verified")},
       EVistaMmg040RootMotionPolicy::Forbidden},
      {EVistaMmg040PinnedAsset::DragMontage,
       TEXT("drag_montage"),
       TEXT("/Game/VISTA/MMG040/Montages/"
            "AM_MMG040_DragChair.AM_MMG040_DragChair"),
       TEXT("/Script/Engine.AnimMontage"),
       EVistaMmg040PinnedAsset::Skeleton,
       {TEXT("vista_drag_distance_reached")},
       EVistaMmg040RootMotionPolicy::Required},
      {EVistaMmg040PinnedAsset::LiftFootMontage,
       TEXT("lift_foot_montage"),
       TEXT("/Game/VISTA/MMG040/Montages/"
            "AM_MMG040_LiftFootHesitate.AM_MMG040_LiftFootHesitate"),
       TEXT("/Script/Engine.AnimMontage"),
       EVistaMmg040PinnedAsset::Skeleton,
       {TEXT("vista_lift_foot_contact_verified")},
       EVistaMmg040RootMotionPolicy::Forbidden},
      {EVistaMmg040PinnedAsset::PauseMontage,
       TEXT("pause_montage"),
       TEXT(
           "/Game/VISTA/MMG040/Montages/AM_MMG040_HoldPose.AM_MMG040_HoldPose"),
       TEXT("/Script/Engine.AnimMontage"),
       EVistaMmg040PinnedAsset::Skeleton,
       {TEXT("vista_pause_completed")},
       EVistaMmg040RootMotionPolicy::Forbidden},
      {EVistaMmg040PinnedAsset::FallMontage,
       TEXT("fall_montage"),
       TEXT("/Game/VISTA/MMG040/Montages/AM_MMG040_Fall.AM_MMG040_Fall"),
       TEXT("/Script/Engine.AnimMontage"),
       EVistaMmg040PinnedAsset::Skeleton,
       {TEXT("vista_fall_landed")},
       EVistaMmg040RootMotionPolicy::Required},
      {EVistaMmg040PinnedAsset::RecoverMontage,
       TEXT("recover_montage"),
       TEXT("/Game/VISTA/MMG040/Montages/AM_MMG040_Recover.AM_MMG040_Recover"),
       TEXT("/Script/Engine.AnimMontage"),
       EVistaMmg040PinnedAsset::Skeleton,
       {TEXT("vista_recover_aligned")},
       EVistaMmg040RootMotionPolicy::Required}};
  return Values;
}

const TArray<FVistaMmg040PinnedActionContract> &
FVistaMmg040ContentDriver::PinnedActions() {
  static const TArray<FVistaMmg040PinnedActionContract> Values = {
      {EVistaAnimationAction::LookAt,
       TEXT("look_at"),
       TEXT("vista_look_at_v1"),
       TEXT("vista_look_at_v1"),
       EVistaMmg040PinnedAsset::LookAtMontage,
       {EVistaMmg040PinnedAsset::AnimBlueprint,
        EVistaMmg040PinnedAsset::ControlRig},
       TEXT("vista_look_at_completed"),
       5000,
       {TEXT("gaze")},
       {TEXT("gaze_target")},
       {TEXT("gaze_target")},
       {TOptional<int32>(1), {}, {}, {}, {}, {}},
       {TEXT("constrained_gaze"), TEXT("completion_notify")}},
      {EVistaAnimationAction::PickUp,
       TEXT("pick_up"),
       TEXT("vista_pick_up_ik_v1"),
       TEXT("vista_pick_up_ik_v1"),
       EVistaMmg040PinnedAsset::PickUpMontage,
       {EVistaMmg040PinnedAsset::AnimBlueprint,
        EVistaMmg040PinnedAsset::ControlRig, EVistaMmg040PinnedAsset::IkRig},
       TEXT("vista_pick_up_attached"),
       8000,
       {TEXT("upper_body_ik"), TEXT("object_attachment")},
       {TEXT("pickupable"), TEXT("hand_contact_target")},
       {TEXT("hand_contact")},
       {TOptional<int32>(2),
        {},
        {},
        TOptional<FString>(FString(TEXT("right"))),
        {},
        {}},
       {TEXT("hand_contact"), TEXT("object_attached"),
        TEXT("completion_notify")}},
      {EVistaAnimationAction::Brace,
       TEXT("brace"),
       TEXT("vista_brace_ik_v1"),
       TEXT("vista_brace_ik_v1"),
       EVistaMmg040PinnedAsset::BraceMontage,
       {EVistaMmg040PinnedAsset::AnimBlueprint,
        EVistaMmg040PinnedAsset::ControlRig, EVistaMmg040PinnedAsset::IkRig},
       TEXT("vista_brace_contact_verified"),
       8000,
       {TEXT("upper_body_ik")},
       {TEXT("hand_contact_target")},
       {TEXT("hand_contact")},
       {TOptional<int32>(2),
        {},
        {},
        TOptional<FString>(FString(TEXT("both"))),
        {},
        {}},
       {TEXT("both_hand_contact"), TEXT("feet_planted"),
        TEXT("completion_notify")}},
      {EVistaAnimationAction::Drag,
       TEXT("drag"),
       TEXT("vista_drag_ik_v1"),
       TEXT("vista_drag_ik_v1"),
       EVistaMmg040PinnedAsset::DragMontage,
       {EVistaMmg040PinnedAsset::AnimBlueprint,
        EVistaMmg040PinnedAsset::ControlRig, EVistaMmg040PinnedAsset::IkRig},
       TEXT("vista_drag_distance_reached"),
       10000,
       {TEXT("root_motion"), TEXT("upper_body_ik")},
       {TEXT("draggable"), TEXT("hand_contact_target")},
       {TEXT("hand_contact")},
       {TOptional<int32>(2),
        TOptional<int32>(120),
        {},
        TOptional<FString>(FString(TEXT("right"))),
        {},
        {}},
       {TEXT("hand_contact"), TEXT("root_motion"), TEXT("caster_physics"),
        TEXT("completion_notify")}},
      {EVistaAnimationAction::LiftFoot,
       TEXT("lift_foot"),
       TEXT("vista_lift_foot_ik_v1"),
       TEXT("vista_lift_foot_ik_v1"),
       EVistaMmg040PinnedAsset::LiftFootMontage,
       {EVistaMmg040PinnedAsset::AnimBlueprint,
        EVistaMmg040PinnedAsset::ControlRig, EVistaMmg040PinnedAsset::IkRig},
       TEXT("vista_lift_foot_contact_verified"),
       8000,
       {TEXT("lower_body_ik")},
       {TEXT("foot_contact_target")},
       {TEXT("foot_contact")},
       {TOptional<int32>(2),
        {},
        TOptional<int32>(35),
        {},
        TOptional<FString>(FString(TEXT("left"))),
        {}},
       {TEXT("foot_contact"), TEXT("no_penetration"),
        TEXT("completion_notify")}},
      {EVistaAnimationAction::Pause,
       TEXT("pause"),
       TEXT("vista_pause_pose_v1"),
       TEXT("vista_pause_pose_v1"),
       EVistaMmg040PinnedAsset::PauseMontage,
       {EVistaMmg040PinnedAsset::AnimBlueprint},
       TEXT("vista_pause_completed"),
       10000,
       {TEXT("hold_pose")},
       {},
       {},
       {TOptional<int32>(3), {}, {}, {}, {}, {}},
       {TEXT("pose_hold"), TEXT("completion_notify")}},
      {EVistaAnimationAction::Fall,
       TEXT("fall"),
       TEXT("vista_fall_montage_v1"),
       TEXT("vista_fall_montage_v1"),
       EVistaMmg040PinnedAsset::FallMontage,
       {EVistaMmg040PinnedAsset::AnimBlueprint},
       TEXT("vista_fall_landed"),
       8000,
       {TEXT("fall_montage")},
       {},
       {},
       {{}, {}, {}, {}, {}, TOptional<FString>(FString(TEXT("forward")))},
       {TEXT("collision_transition"), TEXT("landed_pose"),
        TEXT("completion_notify")}},
      {EVistaAnimationAction::Recover,
       TEXT("recover"),
       TEXT("vista_recover_montage_v1"),
       TEXT("vista_recover_montage_v1"),
       EVistaMmg040PinnedAsset::RecoverMontage,
       {EVistaMmg040PinnedAsset::AnimBlueprint},
       TEXT("vista_recover_aligned"),
       10000,
       {TEXT("recover_montage")},
       {},
       {},
       {{}, {}, {}, {}, {}, TOptional<FString>(FString(TEXT("forward")))},
       {TEXT("root_alignment"), TEXT("capsule_alignment"),
        TEXT("completion_notify")}}};
  return Values;
}

FVistaMmg040ContentDriver::FVistaMmg040ContentDriver(
    const FVistaMmg040VerifiedProfileReceipt &InReceipt,
    TSharedRef<IVistaMmg040ProjectBackend, ESPMode::ThreadSafe> InBackend)
    : Receipt(InReceipt), Backend(InBackend),
      HandleNamespace(
          FGuid::NewGuid().ToString(EGuidFormats::Digits).ToLower()) {}

TSharedPtr<FVistaMmg040ContentDriver, ESPMode::ThreadSafe>
FVistaMmg040ContentDriver::Create(
    const FVistaMmg040VerifiedProfileReceipt &Receipt,
    TSharedRef<IVistaMmg040ProjectBackend, ESPMode::ThreadSafe> Backend,
    FString &OutSafeErrorCode) {
  if (!ValidateReceipt(Receipt, OutSafeErrorCode))
    return nullptr;
  return TSharedPtr<FVistaMmg040ContentDriver, ESPMode::ThreadSafe>(
      new FVistaMmg040ContentDriver(Receipt, Backend));
}

bool FVistaMmg040ContentDriver::ValidateReceipt(
    const FVistaMmg040VerifiedProfileReceipt &Candidate,
    FString &OutSafeErrorCode) {
  if (Candidate.Schema != ReceiptSchema ||
      Candidate.SourceContractSha256 != SourceContractSha256() ||
      Candidate.ProfileId != ProfileId ||
      Candidate.ProfileRevision != ProfileRevision ||
      !IsOpaqueId(Candidate.ContentRevision) ||
      !IsLowerHex(Candidate.ContentDigest, 64) ||
      !IsOpaqueId(Candidate.VerificationReceiptId) ||
      !IsUtcTimestamp(Candidate.VerifiedAtUtc) ||
      Candidate.VerificationStatus != VerificationStatus ||
      !IsOpaqueId(Candidate.VerificationOperatorId) ||
      Candidate.VerificationMethod != VerificationMethod ||
      Candidate.ProjectName != ProjectName ||
      Candidate.EngineVersion != EngineVersion ||
      !IsOpaqueId(Candidate.ProjectRevision) ||
      !IsLowerHex(Candidate.ProjectDescriptorSha256, 64) ||
      !SameStringSet(Candidate.PassedChecks, RequiredReceiptChecks()) ||
      Candidate.Assets.Num() != PinnedAssets().Num() ||
      Candidate.Actions.Num() != PinnedActions().Num()) {
    OutSafeErrorCode = TEXT("ANIMATION_MMG040_RECEIPT_INVALID");
    return false;
  }

  TSet<EVistaMmg040PinnedAsset> SeenAssets;
  for (const FVistaMmg040VerifiedAssetReceipt &Observed : Candidate.Assets) {
    const FVistaMmg040PinnedAssetContract *Pin = FindAsset(Observed.Asset);
    if (!Pin || SeenAssets.Contains(Observed.Asset) || !Observed.bLoaded ||
        Observed.ObjectPath != Pin->ObjectPath ||
        Observed.ObservedClass != Pin->ExpectedClass ||
        !IsLowerHex(Observed.PackageSha256, 64) ||
        !SameOptional(Observed.SkeletonAsset, Pin->SkeletonAsset) ||
        !SameStringSet(Observed.NotifyNames, Pin->RequiredNotifies) ||
        Observed.bRootMotionEnabled !=
            (Pin->RootMotionPolicy == EVistaMmg040RootMotionPolicy::Required)) {
      OutSafeErrorCode = TEXT("ANIMATION_MMG040_ASSET_RECEIPT_MISMATCH");
      return false;
    }
    SeenAssets.Add(Observed.Asset);
  }

  TSet<EVistaAnimationAction> SeenActions;
  for (const FVistaMmg040VerifiedActionReceipt &Observed : Candidate.Actions) {
    const FVistaMmg040PinnedActionContract *Pin = FindAction(Observed.Action);
    if (!Pin || Pin->ActionName != ActionName(Observed.Action) ||
        SeenActions.Contains(Observed.Action) ||
        Observed.ImplementationAsset != Pin->ImplementationAsset ||
        Observed.CompletionSignal == LegacyHandTraceCompletionSignal ||
        Observed.CompletionSignal != Pin->CompletionSignal ||
        !IsLowerHex(Observed.BehaviorEvidenceSha256, 64) ||
        !Observed.bImplementationMatches || !Observed.bSkeletonMatches ||
        !Observed.bCompletionSignalObserved ||
        Observed.bIkContactVerified !=
            ExpectedBehaviorFlag(Observed.Action, TEXT("ik")) ||
        Observed.bObjectAttachmentVerified !=
            ExpectedBehaviorFlag(Observed.Action, TEXT("object_attachment")) ||
        Observed.bRootMotionVerified !=
            ExpectedBehaviorFlag(Observed.Action, TEXT("root_motion")) ||
        Observed.bCollisionVerified !=
            ExpectedBehaviorFlag(Observed.Action, TEXT("collision")) ||
        Observed.bRecoveryAlignmentVerified !=
            ExpectedBehaviorFlag(Observed.Action, TEXT("recovery")) ||
        !SameStringSet(Observed.ObservedLiveChecks, Pin->LiveChecks) ||
        !SameParameters(Observed.VerifiedParameters, Pin->Parameters)) {
      OutSafeErrorCode = TEXT("ANIMATION_MMG040_BEHAVIOR_RECEIPT_MISMATCH");
      return false;
    }
    SeenActions.Add(Observed.Action);
  }

  if (BuildContentDigest(Candidate) != Candidate.ContentDigest) {
    OutSafeErrorCode = TEXT("ANIMATION_MMG040_CONTENT_DIGEST_MISMATCH");
    return false;
  }
  return true;
}

bool FVistaMmg040ContentDriver::ValidateTrustedProfile(
    const FVistaAnimationDriverProfileProof &Proof,
    const TArray<FVistaAnimationDriverTrustedAction> &Actions,
    FString &OutSafeErrorCode) const {
  if (Proof.ProfileId != Receipt.ProfileId ||
      Proof.ProfileRevision != Receipt.ProfileRevision ||
      Proof.ContentRevision != Receipt.ContentRevision ||
      Proof.ContentDigest != Receipt.ContentDigest ||
      Proof.VerificationReceiptId != Receipt.VerificationReceiptId ||
      Actions.IsEmpty() || Actions.Num() > PinnedActions().Num()) {
    OutSafeErrorCode = TEXT("ANIMATION_MMG040_TRUSTED_PROFILE_MISMATCH");
    return false;
  }
  TSet<EVistaAnimationAction> Seen;
  for (const FVistaAnimationDriverTrustedAction &Trusted : Actions) {
    const FVistaMmg040PinnedActionContract *Pin = FindAction(Trusted.Action);
    const FVistaMmg040VerifiedActionReceipt *Verified =
        FindActionReceipt(Receipt, Trusted.Action);
    if (!Pin || !Verified || Seen.Contains(Trusted.Action) ||
        Trusted.AdapterId != Pin->AdapterId ||
        Trusted.BridgeActionId != Pin->BridgeActionId ||
        Trusted.CompletionSignal != Pin->CompletionSignal ||
        Trusted.TimeoutMs != Pin->TimeoutMs ||
        !Verified->bCompletionSignalObserved) {
      OutSafeErrorCode = TEXT("ANIMATION_MMG040_TRUSTED_ACTION_MISMATCH");
      return false;
    }
    Seen.Add(Trusted.Action);
  }
  return true;
}

bool FVistaMmg040ContentDriver::Preflight(
    const FVistaAnimationPreflightInput &Input,
    FVistaAnimationPreflightOutput &Output, FString &OutSafeErrorCode) {
  Output = FVistaAnimationPreflightOutput{};
  {
    FScopeLock Guard(&StateMutex);
    if (bMutationQuarantined) {
      OutSafeErrorCode = TEXT("ANIMATION_MMG040_MUTATION_QUARANTINED");
      return false;
    }
    bPreflightReady = false;
    ReadyActions.Reset();
    ReadyActors.Reset();
    ReadyTargets.Reset();
    ReadyActionTargetPairs.Reset();
  }
  if (Input.RequestedActions.IsEmpty() || Input.ActorBindingIds.IsEmpty()) {
    OutSafeErrorCode = TEXT("ANIMATION_MMG040_PREFLIGHT_INPUT_INVALID");
    return false;
  }
  bool bTargetRequired = false;
  TSet<EVistaAnimationAction> SeenRequestedActions;
  TSet<FString> VerifiedActionTargetPairs;
  for (const EVistaAnimationAction Action : Input.RequestedActions) {
    if (!FindAction(Action) || SeenRequestedActions.Contains(Action)) {
      OutSafeErrorCode = TEXT("ANIMATION_MMG040_ACTION_UNPINNED");
      return false;
    }
    SeenRequestedActions.Add(Action);
    bTargetRequired = bTargetRequired ||
                      Action == EVistaAnimationAction::LookAt ||
                      Action == EVistaAnimationAction::PickUp ||
                      Action == EVistaAnimationAction::Brace ||
                      Action == EVistaAnimationAction::Drag ||
                      Action == EVistaAnimationAction::LiftFoot;
  }
  if (bTargetRequired && Input.TargetBindingIds.IsEmpty()) {
    OutSafeErrorCode = TEXT("ANIMATION_MMG040_TARGET_BINDING_MISSING");
    return false;
  }

  TSet<EVistaMmg040PinnedAsset> VerifiedAssets;
  for (const FVistaMmg040PinnedAssetContract &Pin : PinnedAssets()) {
    const FVistaMmg040VerifiedAssetReceipt *Verified =
        FindAssetReceipt(Receipt, Pin.Asset);
    FVistaMmg040AssetInspection Observed;
    FString BackendError;
    if (!Verified ||
        !Backend->InspectPinnedAsset(Pin.Asset, Observed, BackendError)) {
      OutSafeErrorCode = IsSafeErrorCode(BackendError)
                             ? BackendError
                             : TEXT("ANIMATION_MMG040_PINNED_ASSET_MISSING");
      return false;
    }
    if (!ValidateInspection(Observed, Pin, *Verified, OutSafeErrorCode))
      return false;
    VerifiedAssets.Add(Pin.Asset);
  }
  for (const EVistaAnimationAction Action : Input.RequestedActions) {
    const FVistaMmg040PinnedActionContract &Pin = *FindAction(Action);
    if (!VerifiedAssets.Contains(Pin.ImplementationAsset)) {
      OutSafeErrorCode = TEXT("ANIMATION_MMG040_ACTION_ASSET_UNVERIFIED");
      return false;
    }
    for (const EVistaMmg040PinnedAsset SupportingAsset : Pin.SupportingAssets) {
      if (!VerifiedAssets.Contains(SupportingAsset)) {
        OutSafeErrorCode = TEXT("ANIMATION_MMG040_ACTION_ASSET_UNVERIFIED");
        return false;
      }
    }
  }

  TArray<FString> RequiredActorCapabilities;
  for (const EVistaAnimationAction Action : Input.RequestedActions) {
    const FVistaMmg040PinnedActionContract &Pin = *FindAction(Action);
    for (const FString &Capability : Pin.ActorCapabilities)
      RequiredActorCapabilities.AddUnique(Capability);
  }
  for (const FString &BindingId : Input.ActorBindingIds) {
    FVistaAnimationRuntimeBinding Binding;
    FString BackendError;
    if (!Backend->ResolveActorBinding(BindingId, Binding, BackendError)) {
      OutSafeErrorCode = IsSafeErrorCode(BackendError)
                             ? BackendError
                             : TEXT("ANIMATION_MMG040_ACTOR_BINDING_MISSING");
      return false;
    }
    if (Binding.BindingId != BindingId || !Binding.bAvailable ||
        !Binding.bClassMatches || !Binding.bSkeletonMatches ||
        !ContainsAll(Binding.Capabilities, RequiredActorCapabilities)) {
      OutSafeErrorCode = TEXT("ANIMATION_MMG040_ACTOR_CAPABILITY_MISMATCH");
      return false;
    }
    Output.Actors.Add(MoveTemp(Binding));
  }
  for (const FString &BindingId : Input.TargetBindingIds) {
    FVistaAnimationRuntimeBinding Binding;
    FString BackendError;
    if (!Backend->ResolveTargetBinding(BindingId, Binding, BackendError)) {
      OutSafeErrorCode = IsSafeErrorCode(BackendError)
                             ? BackendError
                             : TEXT("ANIMATION_MMG040_TARGET_BINDING_MISSING");
      return false;
    }
    if (Binding.BindingId != BindingId || !Binding.bAvailable ||
        !Binding.bClassMatches) {
      OutSafeErrorCode = TEXT("ANIMATION_MMG040_TARGET_CAPABILITY_MISMATCH");
      return false;
    }
    Output.Targets.Add(MoveTemp(Binding));
  }
  for (const EVistaAnimationAction Action : Input.RequestedActions) {
    const FVistaMmg040PinnedActionContract &Pin = *FindAction(Action);
    if (Pin.TargetCapabilities.IsEmpty() && Pin.AnchorKinds.IsEmpty())
      continue;
    bool bCompatibleTargetFound = false;
    for (const FVistaAnimationRuntimeBinding &Binding : Output.Targets) {
      if (ContainsAll(Binding.Capabilities, Pin.TargetCapabilities) &&
          ContainsAll(Binding.AnchorKinds, Pin.AnchorKinds)) {
        bCompatibleTargetFound = true;
        VerifiedActionTargetPairs.Add(
            ActionTargetKey(Action, Binding.BindingId));
      }
    }
    if (!bCompatibleTargetFound) {
      OutSafeErrorCode = TEXT("ANIMATION_MMG040_TARGET_CAPABILITY_MISMATCH");
      return false;
    }
  }
  for (const EVistaAnimationAction Action : Input.RequestedActions) {
    FVistaAnimationActionAvailability Availability;
    Availability.Action = Action;
    Availability.bAvailable = true;
    Availability.bImplementationMatches = true;
    Availability.bCompletionSignalAvailable = true;
    Output.Actions.Add(Availability);
  }

  {
    FScopeLock Guard(&StateMutex);
    bPreflightReady = true;
    for (const EVistaAnimationAction Action : Input.RequestedActions)
      ReadyActions.Add(Action);
    for (const FString &BindingId : Input.ActorBindingIds)
      ReadyActors.Add(BindingId);
    for (const FString &BindingId : Input.TargetBindingIds)
      ReadyTargets.Add(BindingId);
    ReadyActionTargetPairs = MoveTemp(VerifiedActionTargetPairs);
  }
  return true;
}

bool FVistaMmg040ContentDriver::IsPreflightBindingAllowed(
    EVistaAnimationAction Action, const FString &ActorBindingId,
    const TOptional<FString> &TargetBindingId,
    FString &OutSafeErrorCode) const {
  FScopeLock Guard(&StateMutex);
  if (!bPreflightReady || !ReadyActions.Contains(Action) ||
      !ReadyActors.Contains(ActorBindingId)) {
    OutSafeErrorCode = TEXT("ANIMATION_MMG040_PREFLIGHT_REQUIRED");
    return false;
  }
  const bool bTargetRequired = Action == EVistaAnimationAction::LookAt ||
                               Action == EVistaAnimationAction::PickUp ||
                               Action == EVistaAnimationAction::Brace ||
                               Action == EVistaAnimationAction::Drag ||
                               Action == EVistaAnimationAction::LiftFoot;
  const bool bTargetForbidden = Action == EVistaAnimationAction::Fall ||
                                Action == EVistaAnimationAction::Recover;
  if ((bTargetRequired && (!TargetBindingId.IsSet() ||
                           !ReadyActionTargetPairs.Contains(ActionTargetKey(
                               Action, TargetBindingId.GetValue())))) ||
      (bTargetForbidden && TargetBindingId.IsSet()) ||
      (!bTargetRequired && !bTargetForbidden && TargetBindingId.IsSet() &&
       !ReadyTargets.Contains(TargetBindingId.GetValue()))) {
    OutSafeErrorCode = TEXT("ANIMATION_MMG040_TARGET_BINDING_MISMATCH");
    return false;
  }
  return true;
}

bool FVistaMmg040ContentDriver::Snapshot(
    EVistaAnimationAction Action, const FString &ActorBindingId,
    const TOptional<FString> &TargetBindingId,
    FVistaAnimationSnapshotOutput &Output, FString &OutSafeErrorCode) {
  if (!IsPreflightBindingAllowed(Action, ActorBindingId, TargetBindingId,
                                 OutSafeErrorCode))
    return false;
  return Backend->Snapshot(Action, ActorBindingId, TargetBindingId, Output,
                           OutSafeErrorCode);
}

bool FVistaMmg040ContentDriver::Start(
    EVistaAnimationAction Action, const FString &ActorBindingId,
    const TOptional<FString> &TargetBindingId,
    const FVistaAnimationActionParameters &Parameters,
    FVistaAnimationStartOutput &Output, FString &OutSafeErrorCode) {
  Output = FVistaAnimationStartOutput{};
  {
    FScopeLock Guard(&StateMutex);
    if (bMutationQuarantined) {
      OutSafeErrorCode = TEXT("ANIMATION_MMG040_MUTATION_QUARANTINED");
      return false;
    }
  }
  if (!IsPreflightBindingAllowed(Action, ActorBindingId, TargetBindingId,
                                 OutSafeErrorCode))
    return false;
  const FVistaMmg040PinnedActionContract *Pin = FindAction(Action);
  if (!Pin || !MatchesRuntimeParameters(Parameters, Pin->Parameters)) {
    OutSafeErrorCode = TEXT("ANIMATION_MMG040_PARAMETERS_UNVERIFIED");
    return false;
  }

  FString ActionHandle;
  {
    FScopeLock Guard(&StateMutex);
    if (bMutationQuarantined) {
      OutSafeErrorCode = TEXT("ANIMATION_MMG040_MUTATION_QUARANTINED");
      return false;
    }
    if (NextHandleSequence > MaxActionHandlesPerDriver) {
      bMutationQuarantined = true;
      bPreflightReady = false;
      OutSafeErrorCode =
          TEXT("ANIMATION_MMG040_ACTION_HANDLE_BUDGET_EXHAUSTED");
      return false;
    }
    ActionHandle = TEXT("vmmg-") + HandleNamespace + TEXT("-") +
                   FString::FromInt(static_cast<int32>(NextHandleSequence++));
    if (!IsOpaqueId(ActionHandle) || ActiveHandles.Contains(ActionHandle)) {
      bMutationQuarantined = true;
      bPreflightReady = false;
      OutSafeErrorCode = TEXT("ANIMATION_MMG040_ACTION_HANDLE_UNAVAILABLE");
      return false;
    }
    ActiveHandles.Add(ActionHandle, Action);
  }

  FVistaMmg040BackendStartOutput BackendOutput;
  bool bStarted = false;
  switch (Action) {
  case EVistaAnimationAction::LookAt:
    bStarted = Backend->StartLookAt(ActorBindingId, TargetBindingId.GetValue(),
                                    ActionHandle, Parameters, BackendOutput,
                                    OutSafeErrorCode);
    break;
  case EVistaAnimationAction::PickUp:
    bStarted = Backend->StartPickUp(ActorBindingId, TargetBindingId.GetValue(),
                                    ActionHandle, Parameters, BackendOutput,
                                    OutSafeErrorCode);
    break;
  case EVistaAnimationAction::Brace:
    bStarted = Backend->StartBrace(ActorBindingId, TargetBindingId.GetValue(),
                                   ActionHandle, Parameters, BackendOutput,
                                   OutSafeErrorCode);
    break;
  case EVistaAnimationAction::Drag:
    bStarted = Backend->StartDrag(ActorBindingId, TargetBindingId.GetValue(),
                                  ActionHandle, Parameters, BackendOutput,
                                  OutSafeErrorCode);
    break;
  case EVistaAnimationAction::LiftFoot:
    bStarted = Backend->StartLiftFoot(
        ActorBindingId, TargetBindingId.GetValue(), ActionHandle, Parameters,
        BackendOutput, OutSafeErrorCode);
    break;
  case EVistaAnimationAction::Pause:
    bStarted =
        Backend->StartPause(ActorBindingId, TargetBindingId, ActionHandle,
                            Parameters, BackendOutput, OutSafeErrorCode);
    break;
  case EVistaAnimationAction::Fall:
    bStarted = Backend->StartFall(ActorBindingId, ActionHandle, Parameters,
                                  BackendOutput, OutSafeErrorCode);
    break;
  case EVistaAnimationAction::Recover:
    bStarted = Backend->StartRecover(ActorBindingId, ActionHandle, Parameters,
                                     BackendOutput, OutSafeErrorCode);
    break;
  }
  if (!bStarted) {
    FScopeLock Guard(&StateMutex);
    ActiveHandles.Remove(ActionHandle);
    if (!IsSafeErrorCode(OutSafeErrorCode))
      OutSafeErrorCode = TEXT("ANIMATION_MMG040_START_REJECTED");
    return false;
  }

  bool bReservationIntact = false;
  {
    FScopeLock Guard(&StateMutex);
    const EVistaAnimationAction *ReservedAction =
        ActiveHandles.Find(ActionHandle);
    bReservationIntact = ReservedAction && *ReservedAction == Action;
  }
  if (!bReservationIntact || !FMath::IsFinite(BackendOutput.EngineTimeSec) ||
      BackendOutput.EngineTimeSec < 0.0 ||
      BackendOutput.EngineTimeSec > MaxEngineTimeSec) {
    FString RollbackError;
    const bool bRolledBack =
        Backend->RollbackFailedStart(Action, ActionHandle, RollbackError);
    FScopeLock Guard(&StateMutex);
    const EVistaAnimationAction *ReservedAction =
        ActiveHandles.Find(ActionHandle);
    const bool bCanRemove = ReservedAction && *ReservedAction == Action;
    if (bRolledBack && bCanRemove) {
      ActiveHandles.Remove(ActionHandle);
      OutSafeErrorCode = TEXT("ANIMATION_MMG040_START_OUTPUT_INVALID");
    } else {
      bMutationQuarantined = true;
      bPreflightReady = false;
      OutSafeErrorCode = TEXT("ANIMATION_MMG040_MUTATION_OUTCOME_UNKNOWN");
    }
    return false;
  }

  Output.ActionHandle = ActionHandle;
  Output.EngineTimeSec = BackendOutput.EngineTimeSec;
  return true;
}

bool FVistaMmg040ContentDriver::Wait(const FString &ActionHandle,
                                     FVistaAnimationWaitOutput &Output,
                                     FString &OutSafeErrorCode) {
  EVistaAnimationAction Action = EVistaAnimationAction::Pause;
  {
    FScopeLock Guard(&StateMutex);
    const EVistaAnimationAction *Found = ActiveHandles.Find(ActionHandle);
    if (!Found) {
      OutSafeErrorCode = TEXT("ANIMATION_MMG040_ACTION_HANDLE_INVALID");
      return false;
    }
    Action = *Found;
  }
  if (!Backend->Wait(ActionHandle, Output, OutSafeErrorCode))
    return false;
  const FVistaMmg040PinnedActionContract *Pin = FindAction(Action);
  if (!Pin || !Output.bCompleted ||
      Output.ObservedCompletionSignal != Pin->CompletionSignal) {
    OutSafeErrorCode = TEXT("ANIMATION_MMG040_COMPLETION_SIGNAL_MISMATCH");
    return false;
  }
  if (!IsOpaqueId(Output.CompletionEvidenceId) ||
      !IsLowerHex(Output.CompletionEvidenceSha256, 64) ||
      Output.EvidenceIds.IsEmpty() || Output.EvidenceIds.Num() > 32) {
    OutSafeErrorCode = TEXT("ANIMATION_MMG040_COMPLETION_EVIDENCE_INVALID");
    return false;
  }
  TSet<FString> SeenEvidence;
  for (const FString &EvidenceId : Output.EvidenceIds) {
    if (!IsOpaqueId(EvidenceId) || SeenEvidence.Contains(EvidenceId)) {
      OutSafeErrorCode = TEXT("ANIMATION_MMG040_COMPLETION_EVIDENCE_INVALID");
      return false;
    }
    SeenEvidence.Add(EvidenceId);
  }
  if (!SeenEvidence.Contains(Output.CompletionEvidenceId)) {
    OutSafeErrorCode = TEXT("ANIMATION_MMG040_COMPLETION_EVIDENCE_INVALID");
    return false;
  }
  return true;
}

bool FVistaMmg040ContentDriver::Stop(const FString &ActionHandle,
                                     const FString &Reason,
                                     FVistaAnimationStopOutput &Output,
                                     FString &OutSafeErrorCode) {
  {
    FScopeLock Guard(&StateMutex);
    if (!ActiveHandles.Contains(ActionHandle)) {
      OutSafeErrorCode = TEXT("ANIMATION_MMG040_ACTION_HANDLE_INVALID");
      return false;
    }
  }
  return Backend->Stop(ActionHandle, Reason, Output, OutSafeErrorCode);
}

bool FVistaMmg040ContentDriver::Release(const FString &ActionHandle,
                                        FVistaAnimationReleaseOutput &Output,
                                        FString &OutSafeErrorCode) {
  {
    FScopeLock Guard(&StateMutex);
    if (!ActiveHandles.Contains(ActionHandle)) {
      OutSafeErrorCode = TEXT("ANIMATION_MMG040_ACTION_HANDLE_INVALID");
      return false;
    }
  }
  if (!Backend->Release(ActionHandle, Output, OutSafeErrorCode))
    return false;
  {
    FScopeLock Guard(&StateMutex);
    if (ActiveHandles.Remove(ActionHandle) != 1) {
      OutSafeErrorCode = TEXT("ANIMATION_MMG040_ACTION_HANDLE_INVALID");
      return false;
    }
  }
  return true;
}

bool FVistaMmg040ContentDriver::Restore(const FString &SnapshotId,
                                        const FString &StateDigest,
                                        FVistaAnimationRestoreOutput &Output,
                                        FString &OutSafeErrorCode) {
  return Backend->Restore(SnapshotId, StateDigest, Output, OutSafeErrorCode);
}

bool FVistaMmg040ContentDriver::CaptureEvidence(
    const FVistaAnimationEvidenceCaptureInput &Input,
    FVistaAnimationEvidenceCaptureOutput &Output, FString &OutSafeErrorCode) {
  {
    FScopeLock Guard(&StateMutex);
    const EVistaAnimationAction Action = Input.Action.IsSet()
                                             ? Input.Action.GetValue()
                                             : EVistaAnimationAction::Pause;
    const bool bTargetRequired =
        Input.Action.IsSet() && (Action == EVistaAnimationAction::LookAt ||
                                 Action == EVistaAnimationAction::PickUp ||
                                 Action == EVistaAnimationAction::Brace ||
                                 Action == EVistaAnimationAction::Drag ||
                                 Action == EVistaAnimationAction::LiftFoot);
    const bool bTargetForbidden =
        Input.Action.IsSet() && (Action == EVistaAnimationAction::Fall ||
                                 Action == EVistaAnimationAction::Recover);
    if (!bPreflightReady ||
        (Input.Action.IsSet() && !ReadyActions.Contains(Action)) ||
        (Input.ActorBindingId.IsSet() &&
         !ReadyActors.Contains(Input.ActorBindingId.GetValue())) ||
        (Input.TargetBindingId.IsSet() &&
         !ReadyTargets.Contains(Input.TargetBindingId.GetValue())) ||
        (bTargetRequired && (!Input.TargetBindingId.IsSet() ||
                             !ReadyActionTargetPairs.Contains(ActionTargetKey(
                                 Action, Input.TargetBindingId.GetValue())))) ||
        (bTargetForbidden && Input.TargetBindingId.IsSet())) {
      OutSafeErrorCode = TEXT("ANIMATION_MMG040_EVIDENCE_UNVERIFIED");
      return false;
    }
  }
  return Backend->CaptureEvidence(Input, Output, OutSafeErrorCode);
}
