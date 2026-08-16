#pragma once

#include "CoreMinimal.h"
#include "Subsystems/WorldSubsystem.h"
#include "VistaPlayableHomeTypes.h"
#include "VistaPlayableHomeRuntimeSubsystem.generated.h"

UENUM(BlueprintType)
enum class EVistaLiveEventOperation : uint8
{
    StartEvent,
    ResetEvent
};

USTRUCT(BlueprintType)
struct VISTAPLAYABLEHOME_API FVistaLiveCommandEnvelope
{
    GENERATED_BODY()

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA|Runtime")
    FName CommandId = NAME_None;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA|Runtime")
    FName ExpectedRevision = NAME_None;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA|Runtime")
    int32 SessionGeneration = 0;
};

USTRUCT(BlueprintType)
struct VISTAPLAYABLEHOME_API FVistaLiveInteractionCommand
{
    GENERATED_BODY()

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA|Runtime")
    FVistaLiveCommandEnvelope Envelope;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA|Runtime")
    FString RequesterSemanticId;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA|Runtime")
    FString TargetSemanticId;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA|Runtime")
    EVistaAffordance Affordance = EVistaAffordance::Inspect;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA|Runtime")
    FString PlacementAnchorSemanticId;
};

USTRUCT(BlueprintType)
struct VISTAPLAYABLEHOME_API FVistaLiveNpcQueueCommand
{
    GENERATED_BODY()

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA|Runtime")
    FVistaLiveCommandEnvelope Envelope;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA|Runtime")
    FString NpcSemanticId;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA|Runtime")
    bool bReplace = true;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA|Runtime")
    TArray<FVistaNpcAction> Actions;
};

USTRUCT(BlueprintType)
struct VISTAPLAYABLEHOME_API FVistaLiveEventCommand
{
    GENERATED_BODY()

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA|Runtime")
    FVistaLiveCommandEnvelope Envelope;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA|Runtime")
    EVistaLiveEventOperation Operation = EVistaLiveEventOperation::StartEvent;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA|Runtime")
    FName EventId = NAME_None;
};

USTRUCT(BlueprintType)
struct VISTAPLAYABLEHOME_API FVistaLiveCommandResult
{
    GENERATED_BODY()

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Runtime")
    FName CommandId = NAME_None;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Runtime")
    bool bSucceeded = false;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Runtime")
    FName Code = TEXT("REJECTED");

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Runtime")
    FString TargetSemanticId;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Runtime")
    FVistaEntityRuntimeState State;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Runtime")
    EVistaEventStatus EventStatus = EVistaEventStatus::Inactive;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Runtime")
    int32 SessionGeneration = 0;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Runtime")
    FName WorldRevision = NAME_None;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Runtime")
    FName ActiveEventId = NAME_None;
};

/**
 * Values read from the active renderer on the game thread.  This is an
 * observation-only structure: no requested or configured values are accepted
 * from the TCP caller.
 */
USTRUCT(BlueprintType)
struct VISTAPLAYABLEHOME_API FVistaRendererRuntimeObservation
{
    GENERATED_BODY()

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Runtime")
    FString UnrealEngineVersion;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Runtime")
    FString Rhi;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Runtime")
    FString FeatureLevel;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Runtime")
    FString ShaderPlatform;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Runtime")
    TMap<FString, double> ConsoleVariables;
};

USTRUCT(BlueprintType)
struct VISTAPLAYABLEHOME_API FVistaRendererStatusResult
{
    GENERATED_BODY()

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Runtime")
    FName CommandId = NAME_None;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Runtime")
    bool bSucceeded = false;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Runtime")
    FName Code = TEXT("RENDERER_OBSERVATION_UNAVAILABLE");

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Runtime")
    FVistaRendererRuntimeObservation Observation;
};

/**
 * Fixed typed boundary for the private Studio/MCP adapter. It intentionally
 * accepts no class, object path, function name, console command, or script.
 */
UCLASS()
class VISTAPLAYABLEHOME_API UVistaPlayableHomeRuntimeSubsystem final
    : public UWorldSubsystem
{
    GENERATED_BODY()

public:
    UFUNCTION(BlueprintPure, Category = "VISTA|Runtime")
    FVistaLiveCommandResult GetStatus(FName CommandId) const;

    /** Read the active RHI and a closed allowlist of effective renderer CVars. */
    FVistaRendererStatusResult GetRendererStatus(FName CommandId) const;

    UFUNCTION(BlueprintCallable, Category = "VISTA|Runtime")
    FVistaLiveCommandResult ExecuteInteraction(
        const FVistaLiveInteractionCommand& Command);

    UFUNCTION(BlueprintCallable, Category = "VISTA|Runtime")
    FVistaLiveCommandResult ExecuteNpcQueue(
        const FVistaLiveNpcQueueCommand& Command);

    UFUNCTION(BlueprintCallable, Category = "VISTA|Runtime")
    FVistaLiveCommandResult ExecuteEvent(
        const FVistaLiveEventCommand& Command);

private:
    bool ValidateEnvelope(const FVistaLiveCommandEnvelope& Envelope,
                          FVistaLiveCommandResult& OutResult) const;
    AActor* ResolveSemanticActor(const FString& SemanticId) const;
};
