#pragma once

#include "CoreMinimal.h"

enum class EVistaAnimationAction : uint8 {
  LookAt,
  Brace,
  Drag,
  LiftFoot,
  Pause,
  Fall,
  Recover
};

struct FVistaAnimationRuntimeBinding {
  FString BindingId;
  bool bAvailable = false;
  bool bClassMatches = false;
  bool bSkeletonMatches = false;
  TArray<FString> Capabilities;
  TArray<FString> AnchorKinds;
};

struct FVistaAnimationActionAvailability {
  EVistaAnimationAction Action = EVistaAnimationAction::Pause;
  bool bAvailable = false;
  bool bImplementationMatches = false;
  bool bCompletionSignalAvailable = false;
};

struct FVistaAnimationPreflightInput {
  FString SceneRevision;
  TArray<EVistaAnimationAction> RequestedActions;
  TArray<FString> ActorBindingIds;
  TArray<FString> TargetBindingIds;
};

struct FVistaAnimationPreflightOutput {
  TArray<FVistaAnimationRuntimeBinding> Actors;
  TArray<FVistaAnimationRuntimeBinding> Targets;
  TArray<FVistaAnimationActionAvailability> Actions;
};

struct FVistaAnimationActionParameters {
  double DurationSec = 0.0;
  double DistanceCm = 0.0;
  double HeightCm = 0.0;
  FString Hand;
  FString Foot;
  FString Direction;
};

struct FVistaAnimationSnapshotOutput {
  FString SnapshotId;
  FString StateDigest;
  double EngineTimeSec = 0.0;
};

struct FVistaAnimationStartOutput {
  FString ActionHandle;
  double EngineTimeSec = 0.0;
};

struct FVistaAnimationWaitOutput {
  bool bCompleted = false;
  double EngineTimeSec = 0.0;
  TArray<FString> EvidenceIds;
};

struct FVistaAnimationStopOutput {
  bool bAlreadyStopped = false;
  double EngineTimeSec = 0.0;
};

struct FVistaAnimationReleaseOutput {
  bool bAlreadyReleased = false;
};

struct FVistaAnimationRestoreOutput {
  double EngineTimeSec = 0.0;
};

/**
 * Trusted, project-owned content implementation.
 *
 * This interface is intentionally typed. Implementations map the fixed enum to
 * packaged AnimBP/Control Rig/montage content. No wire value is ever a class,
 * function, script, console command, filesystem path, or /Game asset path.
 * Methods may be called concurrently (for example Stop while Wait is pending),
 * so the project-owned implementation must provide its own synchronization.
 */
class VISTAANIMATIONCONTENTAPI_API IVistaAnimationContentDriver {
public:
  virtual ~IVistaAnimationContentDriver() = default;

  virtual bool Preflight(const FVistaAnimationPreflightInput &Input,
                         FVistaAnimationPreflightOutput &Output,
                         FString &OutSafeErrorCode) = 0;

  virtual bool Snapshot(EVistaAnimationAction Action,
                        const FString &ActorBindingId,
                        const TOptional<FString> &TargetBindingId,
                        FVistaAnimationSnapshotOutput &Output,
                        FString &OutSafeErrorCode) = 0;

  virtual bool Start(EVistaAnimationAction Action,
                     const FString &ActorBindingId,
                     const TOptional<FString> &TargetBindingId,
                     const FVistaAnimationActionParameters &Parameters,
                     FVistaAnimationStartOutput &Output,
                     FString &OutSafeErrorCode) = 0;

  virtual bool Wait(const FString &ActionHandle,
                    FVistaAnimationWaitOutput &Output,
                    FString &OutSafeErrorCode) = 0;

  virtual bool Stop(const FString &ActionHandle, const FString &Reason,
                    FVistaAnimationStopOutput &Output,
                    FString &OutSafeErrorCode) = 0;

  virtual bool Release(const FString &ActionHandle,
                       FVistaAnimationReleaseOutput &Output,
                       FString &OutSafeErrorCode) = 0;

  virtual bool Restore(const FString &SnapshotId, const FString &StateDigest,
                       FVistaAnimationRestoreOutput &Output,
                       FString &OutSafeErrorCode) = 0;
};
