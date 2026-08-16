#include "VistaPlayableHomeCharacter.h"

#include "Animation/AnimInstance.h"
#include "Camera/CameraComponent.h"
#include "Components/CapsuleComponent.h"
#include "Components/SkeletalMeshComponent.h"
#include "EnhancedInputComponent.h"
#include "EnhancedInputSubsystems.h"
#include "GameFramework/CharacterMovementComponent.h"
#include "GameFramework/PlayerController.h"
#include "GameFramework/SpringArmComponent.h"
#include "InputAction.h"
#include "InputActionValue.h"
#include "InputMappingContext.h"
#include "Engine/SkeletalMesh.h"
#include "Engine/LocalPlayer.h"
#include "Misc/CommandLine.h"
#include "Misc/Parse.h"
#include "UObject/ConstructorHelpers.h"
#include "Net/UnrealNetwork.h"
#include "VistaIndoorSpringArmComponent.h"
#include "VistaInteractable.h"
#include "VistaInteractionComponent.h"
#include "VistaPickupActor.h"

DEFINE_LOG_CATEGORY_STATIC(LogVistaPlayableHomeCamera, Log, All);

namespace
{
const FName RealisticInteriorR2CameraProfileId(TEXT("realistic_interior_r2"));
const TCHAR* CameraProfileCommandLineKey = TEXT("VistaCameraProfile=");
}

FVistaIndoorCameraProfile FVistaIndoorCameraProfile::RealisticInteriorR2()
{
    FVistaIndoorCameraProfile Profile;
    Profile.ProfileId = RealisticInteriorR2CameraProfileId;
    Profile.TargetBoomLengthCm = 220.0f;
    Profile.FieldOfViewDegrees = 80.0f;
    Profile.SocketOffsetCm = FVector(0.0f, 45.0f, 62.0f);
    Profile.CollisionProbeSizeCm = 18.0f;
    Profile.CameraLagSpeed = 14.0f;
    Profile.CameraLagMaxDistanceCm = 30.0f;
    Profile.CollisionRecoverySpeed = 8.0f;
    Profile.RecoverySnapThresholdCm = 1.0f;
    Profile.bEnableCameraCollision = true;
    Profile.bEnableCameraLag = true;
    Profile.bEnableCameraLagSubstepping = true;
    return Profile;
}

bool FVistaIndoorCameraProfile::IsValid(FString& OutReason) const
{
    OutReason.Reset();
    const bool bFinite =
        FMath::IsFinite(TargetBoomLengthCm) &&
        FMath::IsFinite(FieldOfViewDegrees) &&
        !SocketOffsetCm.ContainsNaN() &&
        FMath::IsFinite(CollisionProbeSizeCm) &&
        FMath::IsFinite(CameraLagSpeed) &&
        FMath::IsFinite(CameraLagMaxDistanceCm) &&
        FMath::IsFinite(CollisionRecoverySpeed) &&
        FMath::IsFinite(RecoverySnapThresholdCm);
    if (!bFinite)
    {
        OutReason = TEXT("non_finite_setting");
        return false;
    }
    if (ProfileId != RealisticInteriorR2CameraProfileId)
    {
        OutReason = TEXT("unknown_profile_id");
        return false;
    }
    if (TargetBoomLengthCm < 180.0f || TargetBoomLengthCm > 240.0f)
    {
        OutReason = TEXT("target_boom_out_of_range");
        return false;
    }
    if (FieldOfViewDegrees < 75.0f || FieldOfViewDegrees > 85.0f)
    {
        OutReason = TEXT("field_of_view_out_of_range");
        return false;
    }
    if (!FMath::IsNearlyZero(SocketOffsetCm.X) ||
        FMath::Abs(SocketOffsetCm.Y) > 65.0f ||
        SocketOffsetCm.Z < 45.0f || SocketOffsetCm.Z > 80.0f)
    {
        OutReason = TEXT("socket_offset_out_of_range");
        return false;
    }
    if (CollisionProbeSizeCm < 12.0f || CollisionProbeSizeCm > 24.0f ||
        CameraLagSpeed < 8.0f || CameraLagSpeed > 30.0f ||
        CameraLagMaxDistanceCm < 0.0f || CameraLagMaxDistanceCm > 50.0f ||
        CollisionRecoverySpeed < 4.0f || CollisionRecoverySpeed > 20.0f ||
        RecoverySnapThresholdCm < 0.1f || RecoverySnapThresholdCm > 5.0f)
    {
        OutReason = TEXT("collision_or_lag_setting_out_of_range");
        return false;
    }
    if (!bEnableCameraCollision || !bEnableCameraLag ||
        !bEnableCameraLagSubstepping)
    {
        OutReason = TEXT("required_safety_feature_disabled");
        return false;
    }
    return true;
}

AVistaPlayableHomeCharacter::AVistaPlayableHomeCharacter()
{
    bReplicates = true;
    GetCapsuleComponent()->InitCapsuleSize(42.0f, 96.0f);
    bUseControllerRotationPitch = false;
    bUseControllerRotationYaw = false;
    bUseControllerRotationRoll = false;

    UCharacterMovementComponent* Movement = GetCharacterMovement();
    Movement->bOrientRotationToMovement = true;
    Movement->RotationRate = FRotator(0.0f, 540.0f, 0.0f);
    Movement->MaxWalkSpeed = WalkSpeed;
    Movement->GetNavAgentPropertiesRef().bCanCrouch = true;

    // These are optional project assets: source still compiles when content is
    // absent, while the known prebuilt VISTA project gets a visible Manny pawn.
    static ConstructorHelpers::FObjectFinder<USkeletalMesh> MannyMesh(
        TEXT("/Game/Characters/Mannequins/Meshes/SKM_Manny.SKM_Manny"));
    if (MannyMesh.Succeeded())
    {
        GetMesh()->SetSkeletalMesh(MannyMesh.Object);
        GetMesh()->SetRelativeLocation(FVector(0.0f, 0.0f, -96.0f));
        GetMesh()->SetRelativeRotation(FRotator(0.0f, -90.0f, 0.0f));
    }
    static ConstructorHelpers::FClassFinder<UAnimInstance> MannyAnimBlueprint(
        TEXT("/Game/Characters/Mannequins/Animations/ABP_Manny"));
    if (MannyAnimBlueprint.Succeeded())
    {
        GetMesh()->SetAnimInstanceClass(MannyAnimBlueprint.Class);
    }

    CameraBoom = CreateDefaultSubobject<UVistaIndoorSpringArmComponent>(TEXT("CameraBoom"));
    CameraBoom->SetupAttachment(RootComponent);
    CameraBoom->TargetArmLength = 320.0f;
    CameraBoom->SocketOffset = FVector(0.0f, 55.0f, 65.0f);
    CameraBoom->bUsePawnControlRotation = true;
    CameraBoom->bEnableCameraLag = true;
    CameraBoom->CameraLagSpeed = 12.0f;

    FollowCamera = CreateDefaultSubobject<UCameraComponent>(TEXT("FollowCamera"));
    FollowCamera->SetupAttachment(CameraBoom, USpringArmComponent::SocketName);
    FollowCamera->bUsePawnControlRotation = false;

    CarryAnchor = CreateDefaultSubobject<USceneComponent>(TEXT("VistaCarryAnchor"));
    CarryAnchor->SetupAttachment(FollowCamera);
    CarryAnchor->SetRelativeLocation(FVector(115.0f, 20.0f, -25.0f));
    CarryAnchor->ComponentTags.Add(TEXT("VistaCarryAnchor"));

    InteractionComponent = CreateDefaultSubobject<UVistaInteractionComponent>(TEXT("InteractionComponent"));
}

void AVistaPlayableHomeCharacter::BeginPlay()
{
    Super::BeginPlay();
    GetCharacterMovement()->MaxWalkSpeed = WalkSpeed;
    if (!SemanticId.IsEmpty())
    {
        Tags.AddUnique(FName(*SemanticId));
    }

    ApplyRequestedCameraProfile();

    const APlayerController* PlayerController = Cast<APlayerController>(Controller);
    if (IsValid(PlayerController) && IsValid(DefaultMappingContext))
    {
        if (ULocalPlayer* LocalPlayer = PlayerController->GetLocalPlayer())
        {
            if (UEnhancedInputLocalPlayerSubsystem* Subsystem =
                    LocalPlayer->GetSubsystem<UEnhancedInputLocalPlayerSubsystem>())
            {
                Subsystem->AddMappingContext(DefaultMappingContext, 0);
            }
        }
    }
}

void AVistaPlayableHomeCharacter::ApplyRequestedCameraProfile()
{
    FString RequestedProfileId;
    if (!FParse::Value(
            FCommandLine::Get(),
            CameraProfileCommandLineKey,
            RequestedProfileId))
    {
        // No r2 selection means the accepted r1 camera remains untouched.
        return;
    }
    RequestedProfileId.TrimStartAndEndInline();
    if (!RequestedProfileId.Equals(
            RealisticInteriorR2CameraProfileId.ToString(),
            ESearchCase::CaseSensitive))
    {
        UE_LOG(
            LogVistaPlayableHomeCamera,
            Error,
            TEXT("VISTA_CAMERA_PROFILE_REJECTED unknown profile '%s'; keeping legacy_r1"),
            *RequestedProfileId);
        return;
    }

    if (!ApplyIndoorCameraProfile(FVistaIndoorCameraProfile::RealisticInteriorR2()))
    {
        UE_LOG(
            LogVistaPlayableHomeCamera,
            Error,
            TEXT("VISTA_CAMERA_PROFILE_REJECTED invalid realistic_interior_r2 profile; keeping legacy_r1"));
    }
}

bool AVistaPlayableHomeCharacter::ApplyIndoorCameraProfile(
    const FVistaIndoorCameraProfile& Profile)
{
    FString ValidationReason;
    if (!Profile.IsValid(ValidationReason))
    {
        UE_LOG(
            LogVistaPlayableHomeCamera,
            Error,
            TEXT("VISTA_CAMERA_PROFILE_REJECTED %s; camera settings unchanged"),
            *ValidationReason);
        return false;
    }

    UVistaIndoorSpringArmComponent* IndoorBoom =
        Cast<UVistaIndoorSpringArmComponent>(CameraBoom);
    if (!IsValid(IndoorBoom) || !IsValid(FollowCamera))
    {
        UE_LOG(
            LogVistaPlayableHomeCamera,
            Error,
            TEXT("VISTA_CAMERA_PROFILE_REJECTED required camera components missing"));
        return false;
    }

    // Validation completes before the first mutation, so malformed settings
    // cannot leave the pawn in a partially-applied camera state.
    IndoorBoom->TargetArmLength = Profile.TargetBoomLengthCm;
    IndoorBoom->SocketOffset = Profile.SocketOffsetCm;
    IndoorBoom->bDoCollisionTest = Profile.bEnableCameraCollision;
    IndoorBoom->ProbeSize = Profile.CollisionProbeSizeCm;
    IndoorBoom->ProbeChannel = ECC_Camera;
    IndoorBoom->bEnableCameraLag = Profile.bEnableCameraLag;
    IndoorBoom->CameraLagSpeed = Profile.CameraLagSpeed;
    IndoorBoom->CameraLagMaxDistance = Profile.CameraLagMaxDistanceCm;
    IndoorBoom->bUseCameraLagSubstepping = Profile.bEnableCameraLagSubstepping;
    IndoorBoom->CameraLagMaxTimeStep = 1.0f / 60.0f;
    IndoorBoom->ConfigureIndoorCollisionRecovery(
        Profile.bEnableCameraCollision,
        Profile.CollisionRecoverySpeed,
        Profile.RecoverySnapThresholdCm);
    FollowCamera->SetFieldOfView(Profile.FieldOfViewDegrees);
    ActiveCameraProfileId = Profile.ProfileId;

    UE_LOG(
        LogVistaPlayableHomeCamera,
        Display,
        TEXT("VISTA_CAMERA_PROFILE_ACTIVE id=%s boom_cm=%.1f fov_deg=%.1f"),
        *ActiveCameraProfileId.ToString(),
        IndoorBoom->TargetArmLength,
        FollowCamera->FieldOfView);
    return true;
}

void AVistaPlayableHomeCharacter::EndPlay(const EEndPlayReason::Type EndPlayReason)
{
    SetSprinting(false);
    UnCrouch();
    if (HasAuthority() && IsValid(HeldItem))
    {
        HeldItem->ReleaseFromCarrier();
    }
    Super::EndPlay(EndPlayReason);
}

void AVistaPlayableHomeCharacter::SetupPlayerInputComponent(UInputComponent* PlayerInputComponent)
{
    Super::SetupPlayerInputComponent(PlayerInputComponent);

    if (UEnhancedInputComponent* Enhanced = Cast<UEnhancedInputComponent>(PlayerInputComponent))
    {
        if (IsValid(MoveAction))
        {
            Enhanced->BindAction(MoveAction, ETriggerEvent::Triggered, this, &ThisClass::Move);
        }
        if (IsValid(LookAction))
        {
            Enhanced->BindAction(LookAction, ETriggerEvent::Triggered, this, &ThisClass::Look);
        }
        if (IsValid(JumpAction))
        {
            Enhanced->BindAction(JumpAction, ETriggerEvent::Started, this, &ACharacter::Jump);
            Enhanced->BindAction(JumpAction, ETriggerEvent::Completed, this, &ACharacter::StopJumping);
        }
        if (IsValid(SprintAction))
        {
            Enhanced->BindAction(SprintAction, ETriggerEvent::Started, this, &ThisClass::BeginSprint);
            Enhanced->BindAction(SprintAction, ETriggerEvent::Completed, this, &ThisClass::EndSprint);
            Enhanced->BindAction(SprintAction, ETriggerEvent::Canceled, this, &ThisClass::EndSprint);
        }
        if (IsValid(CrouchAction))
        {
            Enhanced->BindAction(CrouchAction, ETriggerEvent::Started, this, &ThisClass::BeginCrouch);
            Enhanced->BindAction(CrouchAction, ETriggerEvent::Completed, this, &ThisClass::EndCrouch);
            Enhanced->BindAction(CrouchAction, ETriggerEvent::Canceled, this, &ThisClass::EndCrouch);
        }
        if (IsValid(InteractAction))
        {
            Enhanced->BindAction(InteractAction, ETriggerEvent::Started, this, &ThisClass::InteractPressed);
        }
        if (IsValid(DropAction))
        {
            Enhanced->BindAction(DropAction, ETriggerEvent::Started, this, &ThisClass::DropPressed);
        }
    }

    // Legacy bindings keep the C++ pawn usable in a disposable project before
    // Enhanced Input assets are assigned. The project may define either set.
    PlayerInputComponent->BindAxis(TEXT("MoveForward"), this, &ThisClass::MoveForwardLegacy);
    PlayerInputComponent->BindAxis(TEXT("MoveRight"), this, &ThisClass::MoveRightLegacy);
    PlayerInputComponent->BindAxis(TEXT("Turn"), this, &ThisClass::LookYawLegacy);
    PlayerInputComponent->BindAxis(TEXT("LookUp"), this, &ThisClass::LookPitchLegacy);
    PlayerInputComponent->BindAction(TEXT("Jump"), IE_Pressed, this, &ACharacter::Jump);
    PlayerInputComponent->BindAction(TEXT("Jump"), IE_Released, this, &ACharacter::StopJumping);
    PlayerInputComponent->BindAction(TEXT("Sprint"), IE_Pressed, this, &ThisClass::BeginSprint);
    PlayerInputComponent->BindAction(TEXT("Sprint"), IE_Released, this, &ThisClass::EndSprint);
    PlayerInputComponent->BindAction(TEXT("Crouch"), IE_Pressed, this, &ThisClass::BeginCrouch);
    PlayerInputComponent->BindAction(TEXT("Crouch"), IE_Released, this, &ThisClass::EndCrouch);
    PlayerInputComponent->BindAction(TEXT("Interact"), IE_Pressed, this, &ThisClass::InteractPressed);
    PlayerInputComponent->BindAction(TEXT("Drop"), IE_Pressed, this, &ThisClass::DropPressed);
}

void AVistaPlayableHomeCharacter::Move(const FInputActionValue& Value)
{
    const FVector2D MovementVector = Value.Get<FVector2D>();
    const FRotator ControlRotation = Controller ? Controller->GetControlRotation() : FRotator::ZeroRotator;
    const FRotator YawRotation(0.0f, ControlRotation.Yaw, 0.0f);
    AddMovementInput(FRotationMatrix(YawRotation).GetUnitAxis(EAxis::Y), MovementVector.X);
    AddMovementInput(FRotationMatrix(YawRotation).GetUnitAxis(EAxis::X), MovementVector.Y);
}

void AVistaPlayableHomeCharacter::Look(const FInputActionValue& Value)
{
    const FVector2D LookAxis = Value.Get<FVector2D>();
    AddControllerYawInput(LookAxis.X);
    AddControllerPitchInput(LookAxis.Y);
}

void AVistaPlayableHomeCharacter::MoveForwardLegacy(float Value)
{
    if (Controller && !FMath::IsNearlyZero(Value))
    {
        const FRotator Yaw(0.0f, Controller->GetControlRotation().Yaw, 0.0f);
        AddMovementInput(FRotationMatrix(Yaw).GetUnitAxis(EAxis::X), Value);
    }
}

void AVistaPlayableHomeCharacter::MoveRightLegacy(float Value)
{
    if (Controller && !FMath::IsNearlyZero(Value))
    {
        const FRotator Yaw(0.0f, Controller->GetControlRotation().Yaw, 0.0f);
        AddMovementInput(FRotationMatrix(Yaw).GetUnitAxis(EAxis::Y), Value);
    }
}

void AVistaPlayableHomeCharacter::LookYawLegacy(float Value) { AddControllerYawInput(Value); }
void AVistaPlayableHomeCharacter::LookPitchLegacy(float Value) { AddControllerPitchInput(Value); }

void AVistaPlayableHomeCharacter::SetSprinting(bool bEnabled)
{
    bSprinting = bEnabled;
    GetCharacterMovement()->MaxWalkSpeed = bSprinting ? SprintSpeed : WalkSpeed;
}

void AVistaPlayableHomeCharacter::BeginSprint()
{
    SetSprinting(true);
    if (!HasAuthority())
    {
        ServerSetSprinting(true);
    }
}

void AVistaPlayableHomeCharacter::EndSprint()
{
    SetSprinting(false);
    if (!HasAuthority())
    {
        ServerSetSprinting(false);
    }
}

void AVistaPlayableHomeCharacter::ServerSetSprinting_Implementation(bool bEnabled)
{
    SetSprinting(bEnabled);
}

void AVistaPlayableHomeCharacter::BeginCrouch() { Crouch(); }
void AVistaPlayableHomeCharacter::EndCrouch() { UnCrouch(); }

void AVistaPlayableHomeCharacter::InteractPressed()
{
    if (HasAuthority())
    {
        PerformDefaultInteraction();
    }
    else
    {
        ServerPerformDefaultInteraction();
    }
}

void AVistaPlayableHomeCharacter::DropPressed()
{
    if (HasAuthority())
    {
        DropHeldItem();
    }
    else
    {
        ServerDropHeldItem();
    }
}

void AVistaPlayableHomeCharacter::ServerPerformDefaultInteraction_Implementation()
{
    PerformDefaultInteraction();
}

void AVistaPlayableHomeCharacter::ServerDropHeldItem_Implementation()
{
    DropHeldItem();
}

FVistaInteractionResult AVistaPlayableHomeCharacter::PerformDefaultInteraction()
{
    AActor* Target = InteractionComponent->GetFocusedActor();
    if (!IsValid(Target))
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::NotFound, TEXT("NO_INTERACTABLE_TARGET"));
    }

    if (IsValid(HeldItem) && Target != HeldItem)
    {
        FVistaInteractionRequest PlaceRequest;
        PlaceRequest.Affordance = EVistaAffordance::Place;
        PlaceRequest.Requester = this;
        PlaceRequest.PlacementAnchor = Target->GetRootComponent();
        return IVistaInteractable::Execute_VistaInteract(HeldItem, PlaceRequest);
    }
    return InteractionComponent->TryInteract(GetDefaultInteractionAffordance(Target));
}

EVistaAffordance AVistaPlayableHomeCharacter::GetDefaultInteractionAffordance(
    AActor* Target) const
{
    if (!IsValid(Target) || !Target->GetClass()->ImplementsInterface(UVistaInteractable::StaticClass()))
    {
        return EVistaAffordance::Inspect;
    }
    const TArray<EVistaAffordance> Affordances =
        IVistaInteractable::Execute_VistaGetAffordances(Target);
    if (Affordances.Contains(EVistaAffordance::PickUp))
    {
        return EVistaAffordance::PickUp;
    }
    if (Affordances.Contains(EVistaAffordance::Open))
    {
        const FVistaEntityRuntimeState State = IVistaInteractable::Execute_VistaGetRuntimeState(Target);
        const FString* OpenValue = State.Values.Find(TEXT("open"));
        const bool bOpen = OpenValue && OpenValue->Equals(TEXT("true"), ESearchCase::CaseSensitive);
        return bOpen && Affordances.Contains(EVistaAffordance::Close)
            ? EVistaAffordance::Close
            : EVistaAffordance::Open;
    }
    if (Affordances.Contains(EVistaAffordance::Toggle))
    {
        return EVistaAffordance::Toggle;
    }
    if (Affordances.Contains(EVistaAffordance::Sit))
    {
        return EVistaAffordance::Sit;
    }
    return EVistaAffordance::Inspect;
}

FVistaInteractionResult AVistaPlayableHomeCharacter::DropHeldItem()
{
    if (!IsValid(HeldItem))
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::InvalidState, TEXT("NO_HELD_ITEM"));
    }
    const FVector ThrowVelocity = GetVelocity() + GetActorForwardVector() * 120.0f;
    return HeldItem->ReleaseFromCarrier(ThrowVelocity);
}

USceneComponent* AVistaPlayableHomeCharacter::VistaGetCarryAnchor_Implementation() const
{
    return CarryAnchor;
}

AActor* AVistaPlayableHomeCharacter::VistaGetHeldItem_Implementation() const
{
    return HeldItem;
}

bool AVistaPlayableHomeCharacter::VistaTryClaimItem_Implementation(AActor* Item)
{
    AVistaPickupActor* Pickup = Cast<AVistaPickupActor>(Item);
    if (!IsValid(Pickup) || IsValid(HeldItem))
    {
        return false;
    }
    HeldItem = Pickup;
    return true;
}

void AVistaPlayableHomeCharacter::VistaReleaseItem_Implementation(AActor* Item)
{
    if (HeldItem == Item)
    {
        HeldItem = nullptr;
    }
}

void AVistaPlayableHomeCharacter::GetLifetimeReplicatedProps(
    TArray<FLifetimeProperty>& OutLifetimeProps) const
{
    Super::GetLifetimeReplicatedProps(OutLifetimeProps);
    DOREPLIFETIME(AVistaPlayableHomeCharacter, HeldItem);
}
