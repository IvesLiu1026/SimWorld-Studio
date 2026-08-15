#pragma once

#include "AIController.h"
#include "CoreMinimal.h"
#include "Navigation/PathFollowingComponent.h"
#include "VistaPlayableHomeTypes.h"
#include "VistaHomeNpcController.generated.h"

DECLARE_DYNAMIC_MULTICAST_DELEGATE_OneParam(FVistaNpcActionFinished,
                                            const FVistaNpcActionResult&, Result);
DECLARE_DYNAMIC_MULTICAST_DELEGATE_TwoParams(FVistaNpcSpoke,
                                             const FString&, NpcSemanticId,
                                             const FString&, Speech);

UCLASS(Blueprintable)
class VISTAPLAYABLEHOME_API AVistaHomeNpcController final : public AAIController
{
    GENERATED_BODY()

public:
    AVistaHomeNpcController();

    UPROPERTY(BlueprintAssignable, Category = "VISTA|NPC")
    FVistaNpcActionFinished OnActionFinished;

    UPROPERTY(BlueprintAssignable, Category = "VISTA|NPC")
    FVistaNpcSpoke OnNpcSpoke;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|NPC",
              meta = (ClampMin = "1", ClampMax = "64"))
    int32 MaxQueueDepth = 32;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|NPC",
              meta = (ClampMin = "10.0", ClampMax = "250.0"))
    float NavigationAcceptanceRadius = 75.0f;

    UFUNCTION(BlueprintCallable, Category = "VISTA|NPC")
    bool ReplaceActionQueue(const TArray<FVistaNpcAction>& Actions, FName& OutCode);

    UFUNCTION(BlueprintCallable, Category = "VISTA|NPC")
    bool EnqueueAction(const FVistaNpcAction& Action, FName& OutCode);

    UFUNCTION(BlueprintCallable, Category = "VISTA|NPC")
    void CancelActionQueue(FName Reason = TEXT("QUEUE_CANCELED"));

    UFUNCTION(BlueprintCallable, Category = "VISTA|NPC")
    void ConfigurePatrol(const TArray<FString>& TargetSemanticIds,
                         float ActionTimeoutSeconds,
                         bool bEnabled = true);

    UFUNCTION(BlueprintPure, Category = "VISTA|NPC")
    FVistaNpcActionResult GetCurrentActionResult() const { return CurrentResult; }

    UFUNCTION(BlueprintPure, Category = "VISTA|NPC")
    int32 GetQueuedActionCount() const { return ActionQueue.Num(); }

    virtual void Tick(float DeltaSeconds) override;

protected:
    virtual void OnPossess(APawn* InPawn) override;
    virtual void OnMoveCompleted(FAIRequestID RequestId,
                                 const FPathFollowingResult& Result) override;

private:
    TArray<FVistaNpcAction> ActionQueue;
    TOptional<FVistaNpcAction> CurrentAction;
    FVistaNpcActionResult CurrentResult;
    double ActionStartedAt = 0.0;
    bool bActionStarted = false;
    bool bAutoPatrol = false;
    TArray<FString> PatrolTargetSemanticIds;
    int32 PatrolTargetIndex = 0;
    uint64 PatrolSequence = 0;
    float PatrolActionTimeoutSeconds = 20.0f;

    bool ValidateAction(const FVistaNpcAction& Action, FName& OutCode) const;
    void StartNextAction();
    void StartCurrentAction();
    void CompleteCurrent(EVistaNpcActionStatus Status, FName Code);
    AActor* ResolveSemanticActor(const FString& SemanticId) const;
    FVistaInteractionResult ExecuteInteraction(AActor* Target, EVistaAffordance Affordance,
                                                USceneComponent* PlacementAnchor = nullptr) const;
};
