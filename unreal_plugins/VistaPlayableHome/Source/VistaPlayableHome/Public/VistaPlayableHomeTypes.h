#pragma once

#include "CoreMinimal.h"
#include "Templates/SubclassOf.h"
#include "VistaPlayableHomeTypes.generated.h"

class AActor;
class USceneComponent;

UENUM(BlueprintType)
enum class EVistaAffordance : uint8
{
    Open,
    Close,
    PickUp,
    Drop,
    Place,
    Toggle,
    Sit,
    Inspect
};

UENUM(BlueprintType)
enum class EVistaInteractionStatus : uint8
{
    Succeeded,
    Unsupported,
    InvalidRequester,
    InvalidState,
    Busy,
    Blocked,
    NotFound,
    TimedOut,
    RevisionMismatch,
    Rejected
};

USTRUCT(BlueprintType)
struct VISTAPLAYABLEHOME_API FVistaEntityRuntimeState
{
    GENERATED_BODY()

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    FString SemanticId;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    FTransform Transform = FTransform::Identity;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    bool bHidden = false;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    bool bPortable = false;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    TMap<FName, FString> Values;
};

USTRUCT(BlueprintType)
struct VISTAPLAYABLEHOME_API FVistaInteractionRequest
{
    GENERATED_BODY()

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    EVistaAffordance Affordance = EVistaAffordance::Inspect;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    TObjectPtr<AActor> Requester = nullptr;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    TObjectPtr<USceneComponent> PlacementAnchor = nullptr;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    FName ExpectedRevision = NAME_None;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    int32 SessionGeneration = 0;
};

USTRUCT(BlueprintType)
struct VISTAPLAYABLEHOME_API FVistaInteractionResult
{
    GENERATED_BODY()

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA")
    EVistaInteractionStatus Status = EVistaInteractionStatus::Rejected;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA")
    FName Code = TEXT("REJECTED");

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA")
    FString SemanticId;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA")
    FVistaEntityRuntimeState State;

    bool IsSuccess() const { return Status == EVistaInteractionStatus::Succeeded; }

    static FVistaInteractionResult Success(const FString& InSemanticId,
                                           const FVistaEntityRuntimeState& InState,
                                           FName InCode = TEXT("OK"));
    static FVistaInteractionResult Failure(EVistaInteractionStatus InStatus,
                                           FName InCode,
                                           const FString& InSemanticId = FString());
};

UENUM(BlueprintType)
enum class EVistaNpcActionType : uint8
{
    NavigateTo,
    LookAt,
    PickUp,
    Place,
    OpenDoor,
    CloseDoor,
    Sit,
    Wait,
    Speak
};

UENUM(BlueprintType)
enum class EVistaNpcActionStatus : uint8
{
    Idle,
    Queued,
    Running,
    Succeeded,
    Failed,
    TimedOut,
    Blocked
};

USTRUCT(BlueprintType)
struct VISTAPLAYABLEHOME_API FVistaNpcAction
{
    GENERATED_BODY()

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    FName ActionId = NAME_None;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    EVistaNpcActionType Type = EVistaNpcActionType::Wait;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    FString TargetSemanticId;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    FVector TargetLocation = FVector::ZeroVector;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA", meta = (ClampMin = "0.0", ClampMax = "300.0"))
    float DurationSeconds = 0.0f;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA", meta = (ClampMin = "0.0", ClampMax = "300.0"))
    float TimeoutSeconds = 10.0f;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    FString Speech;
};

USTRUCT(BlueprintType)
struct VISTAPLAYABLEHOME_API FVistaNpcActionResult
{
    GENERATED_BODY()

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA")
    FName ActionId = NAME_None;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA")
    EVistaNpcActionStatus Status = EVistaNpcActionStatus::Idle;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA")
    FName Code = NAME_None;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA")
    FString TargetSemanticId;
};

UENUM(BlueprintType)
enum class EVistaEventOperationType : uint8
{
    SpawnFixture,
    SetTransform,
    SetState,
    SetVisibility,
    SetPortable,
    SetNpcQueue,
    SetGoal
};

USTRUCT(BlueprintType)
struct VISTAPLAYABLEHOME_API FVistaEventOperation
{
    GENERATED_BODY()

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    FName OperationId = NAME_None;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    EVistaEventOperationType Type = EVistaEventOperationType::SetState;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    FString TargetSemanticId;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    FTransform Transform = FTransform::Identity;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    TMap<FName, FString> StateValues;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    bool bBooleanValue = false;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    TArray<FVistaNpcAction> NpcActions;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    TSubclassOf<AActor> FixtureClass;
};

USTRUCT(BlueprintType)
struct VISTAPLAYABLEHOME_API FVistaEventDefinition
{
    GENERATED_BODY()

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    FName EventId = NAME_None;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    FName CompatibleRevision = NAME_None;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    FString PublicTitle;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    FString PublicGoal;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA", meta = (ClampMin = "0.1", ClampMax = "3600.0"))
    float TimeoutSeconds = 300.0f;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA")
    TArray<FVistaEventOperation> InitialOperations;
};

UENUM(BlueprintType)
enum class EVistaEventStatus : uint8
{
    Inactive,
    Applying,
    Active,
    Succeeded,
    Failed,
    TimedOut,
    Resetting
};
