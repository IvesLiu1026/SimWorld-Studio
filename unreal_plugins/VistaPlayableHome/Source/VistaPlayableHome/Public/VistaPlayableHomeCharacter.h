#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Character.h"
#include "VistaItemCarrier.h"
#include "VistaPlayableHomeTypes.h"
#include "VistaPlayableHomeCharacter.generated.h"

class AVistaPickupActor;
class UCameraComponent;
class UInputAction;
class UInputMappingContext;
class USceneComponent;
class USpringArmComponent;
class UVistaInteractionComponent;
struct FInputActionValue;

UCLASS(Blueprintable)
class VISTAPLAYABLEHOME_API AVistaPlayableHomeCharacter final
    : public ACharacter,
      public IVistaItemCarrier
{
    GENERATED_BODY()

public:
    AVistaPlayableHomeCharacter();

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Identity")
    FString SemanticId = TEXT("home.r1/player.01");

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Camera")
    TObjectPtr<USpringArmComponent> CameraBoom;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Camera")
    TObjectPtr<UCameraComponent> FollowCamera;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Interaction")
    TObjectPtr<UVistaInteractionComponent> InteractionComponent;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Carry")
    TObjectPtr<USceneComponent> CarryAnchor;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Input")
    TObjectPtr<UInputMappingContext> DefaultMappingContext;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Input")
    TObjectPtr<UInputAction> MoveAction;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Input")
    TObjectPtr<UInputAction> LookAction;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Input")
    TObjectPtr<UInputAction> JumpAction;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Input")
    TObjectPtr<UInputAction> SprintAction;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Input")
    TObjectPtr<UInputAction> CrouchAction;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Input")
    TObjectPtr<UInputAction> InteractAction;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Input")
    TObjectPtr<UInputAction> DropAction;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Movement")
    float WalkSpeed = 350.0f;

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Movement")
    float SprintSpeed = 600.0f;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "VISTA|Location")
    FString CurrentRoomId;

    UFUNCTION(BlueprintPure, Category = "VISTA|Carry")
    AVistaPickupActor* GetHeldPickup() const { return HeldItem; }

    UFUNCTION(BlueprintCallable, Category = "VISTA|Interaction")
    FVistaInteractionResult PerformDefaultInteraction();

    UFUNCTION(BlueprintCallable, Category = "VISTA|Carry")
    FVistaInteractionResult DropHeldItem();

    virtual USceneComponent* VistaGetCarryAnchor_Implementation() const override;
    virtual AActor* VistaGetHeldItem_Implementation() const override;
    virtual bool VistaTryClaimItem_Implementation(AActor* Item) override;
    virtual void VistaReleaseItem_Implementation(AActor* Item) override;

    virtual void GetLifetimeReplicatedProps(
        TArray<FLifetimeProperty>& OutLifetimeProps) const override;

protected:
    virtual void BeginPlay() override;
    virtual void EndPlay(const EEndPlayReason::Type EndPlayReason) override;
    virtual void SetupPlayerInputComponent(UInputComponent* PlayerInputComponent) override;

private:
    UPROPERTY(Replicated)
    TObjectPtr<AVistaPickupActor> HeldItem = nullptr;

    bool bSprinting = false;

    void Move(const FInputActionValue& Value);
    void Look(const FInputActionValue& Value);
    void SetSprinting(bool bEnabled);
    void BeginSprint();
    void EndSprint();
    void BeginCrouch();
    void EndCrouch();
    void InteractPressed();
    void DropPressed();
    void MoveForwardLegacy(float Value);
    void MoveRightLegacy(float Value);
    void LookYawLegacy(float Value);
    void LookPitchLegacy(float Value);
    EVistaAffordance ChooseDefaultAffordance(AActor* Target) const;

    UFUNCTION(Server, Reliable)
    void ServerPerformDefaultInteraction();

    UFUNCTION(Server, Reliable)
    void ServerDropHeldItem();

    UFUNCTION(Server, Reliable)
    void ServerSetSprinting(bool bEnabled);
};
