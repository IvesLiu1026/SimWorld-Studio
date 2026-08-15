#pragma once

#include "CoreMinimal.h"
#include "Components/ActorComponent.h"
#include "VistaPlayableHomeTypes.h"
#include "VistaInteractionComponent.generated.h"

DECLARE_DYNAMIC_MULTICAST_DELEGATE_TwoParams(
    FVistaFocusChanged, AActor*, PreviousActor, AActor*, FocusedActor);

UCLASS(ClassGroup = (VISTA), meta = (BlueprintSpawnableComponent))
class VISTAPLAYABLEHOME_API UVistaInteractionComponent final : public UActorComponent
{
    GENERATED_BODY()

public:
    UVistaInteractionComponent();

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA|Interaction",
              meta = (ClampMin = "50.0", ClampMax = "1000.0"))
    float InteractionDistance = 250.0f;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA|Interaction")
    TEnumAsByte<ECollisionChannel> TraceChannel = ECC_Visibility;

    UPROPERTY(BlueprintAssignable, Category = "VISTA|Interaction")
    FVistaFocusChanged OnFocusChanged;

    UFUNCTION(BlueprintPure, Category = "VISTA|Interaction")
    AActor* GetFocusedActor() const { return FocusedActor.Get(); }

    UFUNCTION(BlueprintPure, Category = "VISTA|Interaction")
    FString GetFocusedSemanticId() const;

    UFUNCTION(BlueprintCallable, Category = "VISTA|Interaction")
    FVistaInteractionResult TryInteract(EVistaAffordance Affordance,
                                        USceneComponent* PlacementAnchor = nullptr);

    UFUNCTION(BlueprintCallable, Category = "VISTA|Interaction")
    void SetExpectedRevision(FName Revision) { ExpectedRevision = Revision; }

protected:
    virtual void BeginPlay() override;
    virtual void TickComponent(float DeltaTime, ELevelTick TickType,
                               FActorComponentTickFunction* ThisTickFunction) override;

private:
    TWeakObjectPtr<AActor> FocusedActor;
    FName ExpectedRevision = NAME_None;
    int32 SessionGeneration = 0;

    AActor* TraceForInteractable() const;
    void UpdateFocus(AActor* NewFocus);
};
