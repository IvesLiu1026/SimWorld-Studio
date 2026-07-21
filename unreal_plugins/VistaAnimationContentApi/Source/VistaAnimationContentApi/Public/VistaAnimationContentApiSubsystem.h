#pragma once

// clang-format off
#include "CoreMinimal.h"
#include "Subsystems/EngineSubsystem.h"
#include "VistaAnimationContentDriver.h"
#include "VistaAnimationContentApiSubsystem.generated.h"
// clang-format on

struct FVistaAnimationSlotBinding {
  FString OwnerId;
  FString SessionId;
  FString SlotId;
  FString SceneRevision;
};

struct FVistaAnimationContentProof {
  FString ProfileId;
  FString ProfileRevision;
  FString ContentRevision;
  FString ContentDigest;
  FString VerificationReceiptId;
};

struct FVistaAnimationTrustedAction {
  EVistaAnimationAction Action = EVistaAnimationAction::Pause;
  FString AdapterId;
  FString BridgeActionId;
  FString CompletionSignal;
  int32 TimeoutMs = 0;
};

/**
 * Supplied only by a trusted project/launcher module before the listener is
 * exposed. It contains no transport credential and is never populated from a
 * browser, NLP prompt, generic MCP payload, or content API request.
 */
struct FVistaAnimationTrustedRuntimeConfig {
  FVistaAnimationSlotBinding SlotBinding;
  FVistaAnimationContentProof ContentProof;
  TArray<FVistaAnimationTrustedAction> Actions;
};

enum class EVistaAnimationFixedDispatchResult : uint8 {
  Handled,
  RejectedUnknownCommand,
  Unavailable
};

UCLASS()
class VISTAANIMATIONCONTENTAPI_API UVistaAnimationContentApiSubsystem final
    : public UEngineSubsystem {
  GENERATED_BODY()

public:
  UVistaAnimationContentApiSubsystem();
  virtual ~UVistaAnimationContentApiSubsystem() override;

  virtual void Initialize(FSubsystemCollectionBase &Collection) override;
  virtual void Deinitialize() override;

  /** Configure once per process. A second call always fails closed. */
  bool ConfigureTrustedRuntime(
      const FVistaAnimationTrustedRuntimeConfig &Config,
      TSharedRef<IVistaAnimationContentDriver, ESPMode::ThreadSafe> Driver,
      FString &OutSafeErrorCode);

  /**
   * Exact dispatcher for a private host listener. It handles only the four
   * literal reserved command types and never falls through to a generic bridge.
   */
  EVistaAnimationFixedDispatchResult
  DispatchFixedJsonCommand(const FString &CommandType,
                           const FString &RequestJson,
                           FString &OutResponseJson);

  bool HandleCapabilityProbeJson(const FString &RequestJson,
                                 FString &OutResponseJson);
  bool HandleContentRequestJson(const FString &RequestJson,
                                FString &OutResponseJson);
  bool HandleEngineTimeJson(const FString &RequestJson,
                            FString &OutResponseJson);
  bool HandleEvidenceCaptureJson(const FString &RequestJson,
                                 FString &OutResponseJson);

private:
  class FImplementation;
  struct FImplementationDeleter {
    void operator()(FImplementation *Instance) const;
  };
  TUniquePtr<FImplementation, FImplementationDeleter> Implementation;
};
