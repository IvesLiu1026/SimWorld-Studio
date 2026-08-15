#pragma once

#include "CoreMinimal.h"
#include "Subsystems/WorldSubsystem.h"
#include "VistaPlayableHomeTypes.h"
#include "VistaEventSubsystem.generated.h"

UCLASS()
class VISTAPLAYABLEHOME_API UVistaEventSubsystem final : public UTickableWorldSubsystem
{
    GENERATED_BODY()

public:
    UFUNCTION(BlueprintCallable, Category = "VISTA|Event")
    void InitializeWorldRevision(FName Revision);

    UFUNCTION(BlueprintCallable, Category = "VISTA|Event")
    bool RegisterEventDefinitions(const TArray<FVistaEventDefinition>& Definitions,
                                  FName& OutCode);

    UFUNCTION(BlueprintCallable, Category = "VISTA|Event")
    bool StartEvent(FName EventId, FName ExpectedRevision,
                    int32 ExpectedGeneration, FName& OutCode);

    UFUNCTION(BlueprintCallable, Category = "VISTA|Event")
    bool ResetEvent(FName ExpectedRevision, int32 ExpectedGeneration,
                    FName& OutCode);

    UFUNCTION(BlueprintPure, Category = "VISTA|Event")
    FName GetWorldRevision() const { return WorldRevision; }

    UFUNCTION(BlueprintPure, Category = "VISTA|Event")
    int32 GetSessionGeneration() const { return SessionGeneration; }

    UFUNCTION(BlueprintPure, Category = "VISTA|Event")
    FName GetActiveEventId() const { return ActiveEventId; }

    UFUNCTION(BlueprintPure, Category = "VISTA|Event")
    EVistaEventStatus GetEventStatus() const { return EventStatus; }

    UFUNCTION(BlueprintPure, Category = "VISTA|Event")
    FString GetPublicGoal() const { return ActivePublicGoal; }

    /** Advance exactly once after a successful broker-owned live mutation. */
    bool CommitCommandGeneration(int32 ExpectedGeneration, int32& OutGeneration);

    virtual void Tick(float DeltaTime) override;
    virtual TStatId GetStatId() const override;

private:
    FName WorldRevision = NAME_None;
    int32 SessionGeneration = 0;
    TMap<FName, FVistaEventDefinition> EventDefinitions;
    FName ActiveEventId = NAME_None;
    FString ActivePublicGoal;
    EVistaEventStatus EventStatus = EVistaEventStatus::Inactive;
    double EventStartedAt = 0.0;
    float ActiveTimeoutSeconds = 0.0f;
    TMap<TWeakObjectPtr<AActor>, FVistaEntityRuntimeState> BaselineStates;
    TArray<TWeakObjectPtr<AActor>> SpawnedFixtures;
    TArray<TWeakObjectPtr<class AVistaHomeNpcController>> ModifiedNpcControllers;

    bool ValidateDefinition(const FVistaEventDefinition& Definition, FName& OutCode) const;
    bool ApplyOperation(const FVistaEventOperation& Operation, FName& OutCode);
    void RestoreBaseline();
    AActor* ResolveSemanticActor(const FString& SemanticId) const;
    bool ValidateEnvelope(FName ExpectedRevision, int32 ExpectedGeneration,
                          FName& OutCode) const;
};
