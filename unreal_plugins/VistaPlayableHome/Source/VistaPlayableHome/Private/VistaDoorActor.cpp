#include "VistaDoorActor.h"

#include "CollisionQueryParams.h"
#include "CollisionShape.h"
#include "Components/PrimitiveComponent.h"
#include "Components/SceneComponent.h"
#include "Components/StaticMeshComponent.h"
#include "Engine/OverlapResult.h"
#include "Engine/StaticMesh.h"
#include "Engine/World.h"
#include "GameFramework/Controller.h"
#include "GameFramework/Pawn.h"
#include "AI/Navigation/NavLinkDefinition.h"
#include "NavAreas/NavArea_Default.h"
#include "NavAreas/NavArea_Null.h"
#include "NavLinkCustomComponent.h"
#include "Navigation/PathFollowingComponent.h"
#include "NavigationSystem.h"
#include "Net/UnrealNetwork.h"

AVistaDoorActor::AVistaDoorActor()
{
    PrimaryActorTick.bCanEverTick = true;
    SceneRoot = CreateDefaultSubobject<USceneComponent>(TEXT("SceneRoot"));
    SetRootComponent(SceneRoot);
    Hinge = CreateDefaultSubobject<USceneComponent>(TEXT("Hinge"));
    Hinge->SetupAttachment(SceneRoot);
    DoorMesh = CreateDefaultSubobject<UStaticMeshComponent>(TEXT("DoorMesh"));
    DoorMesh->SetupAttachment(Hinge);
    DoorMesh->SetCollisionProfileName(TEXT("BlockAllDynamic"));
    DoorMesh->SetGenerateOverlapEvents(true);
    // Navigation is governed by the fixed doorway obstacle and smart link
    // below, independently of the imported leaf's changing collision bounds.
    DoorMesh->SetCanEverAffectNavigation(false);
    DoorwayLink = CreateDefaultSubobject<UNavLinkCustomComponent>(TEXT("DoorwayLink"));
    DoorwayLink->SetLinkData(
        FVector(0.0f, -75.0f, 5.0f),
        FVector(0.0f, 75.0f, 5.0f),
        ENavLinkDirection::BothWays);
    DoorwayLink->SetEnabledArea(UNavArea_Default::StaticClass());
    DoorwayLink->SetDisabledArea(UNavArea_Null::StaticClass());
    // Force separate navmesh islands beneath the door while leaving both
    // endpoints outside the cut.  UE documents this obstacle specifically for
    // empty area under doors; enabling the smart link is then the only route.
    DoorwayLink->AddNavigationObstacle(
        UNavArea_Null::StaticClass(),
        FVector(55.0f, 20.0f, 100.0f),
        FVector(0.0f, 0.0f, 100.0f));
    DoorwayLink->SetMoveReachedLink(this, &AVistaDoorActor::HandleDoorwayLinkReached);
    DoorwayLink->SetEnabled(false);
    AllowedAffordances = {
        EVistaAffordance::Inspect,
        EVistaAffordance::Open,
        EVistaAffordance::Close};
}

void AVistaDoorActor::BeginPlay()
{
    Super::BeginPlay();
    ConfigureJambPivot();
    ClosedRotation = Hinge->GetRelativeRotation();
    if (HasAuthority())
    {
        bOpen = bInitiallyOpen;
    }
    ApplyDoorState(true);
}

void AVistaDoorActor::ConfigureJambPivot()
{
    const UStaticMesh* Mesh = DoorMesh->GetStaticMesh();
    if (!IsValid(Mesh))
    {
        return;
    }
    // Authored door transforms are doorway-centred.  Moving the hinge to the
    // mesh's local minimum-X jamb and counter-offsetting the leaf preserves
    // that closed transform while making the sweep rotate about a true edge.
    const float JambX = Mesh->GetBoundingBox().Min.X;
    Hinge->SetRelativeLocation(FVector(JambX, 0.0f, 0.0f));
    DoorMesh->SetRelativeLocation(FVector(-JambX, 0.0f, 0.0f));
}

void AVistaDoorActor::Tick(float DeltaSeconds)
{
    Super::Tick(DeltaSeconds);
    const FRotator Current = Hinge->GetRelativeRotation();
    Hinge->SetRelativeRotation(FMath::RInterpConstantTo(
        Current, TargetRotation, DeltaSeconds, AngularSpeedDegrees));
}

void AVistaDoorActor::GetLifetimeReplicatedProps(
    TArray<FLifetimeProperty>& OutLifetimeProps) const
{
    Super::GetLifetimeReplicatedProps(OutLifetimeProps);
    DOREPLIFETIME(AVistaDoorActor, bOpen);
}

FVistaEntityRuntimeState AVistaDoorActor::VistaGetRuntimeState_Implementation() const
{
    FVistaEntityRuntimeState State = Super::VistaGetRuntimeState_Implementation();
    State.Values.Add(TEXT("open"), bOpen ? TEXT("true") : TEXT("false"));
    return State;
}

FVistaInteractionResult AVistaDoorActor::VistaApplyRuntimeState_Implementation(
    const FVistaEntityRuntimeState& State)
{
    if (!HasAuthority())
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::Rejected, TEXT("AUTHORITY_REQUIRED"), SemanticId);
    }
    const FString* OpenValue = State.Values.Find(TEXT("open"));
    const bool bRequestedOpen = OpenValue
        ? OpenValue->Equals(TEXT("true"), ESearchCase::CaseSensitive)
        : bOpen;
    if (!bRequestedOpen && bOpen && IsClosingObstructed())
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::Blocked, TEXT("DOOR_SWEEP_OBSTRUCTED"), SemanticId);
    }
    const FVistaInteractionResult BaseResult = Super::VistaApplyRuntimeState_Implementation(State);
    if (!BaseResult.IsSuccess())
    {
        return BaseResult;
    }
    bOpen = bRequestedOpen;
    ApplyDoorState(false);
    ForceNetUpdate();
    return FVistaInteractionResult::Success(
        SemanticId, VistaGetRuntimeState_Implementation(), TEXT("DOOR_STATE_APPLIED"));
}

FVistaInteractionResult AVistaDoorActor::VistaInteract_Implementation(
    const FVistaInteractionRequest& Request)
{
    const FVistaInteractionResult Validation = ValidateRequest(Request);
    if (!Validation.IsSuccess())
    {
        return Validation;
    }
    if (!HasAuthority())
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::Rejected, TEXT("AUTHORITY_REQUIRED"), SemanticId);
    }
    if (Request.Affordance == EVistaAffordance::Open)
    {
        if (bOpen)
        {
            return FVistaInteractionResult::Failure(
                EVistaInteractionStatus::InvalidState, TEXT("DOOR_ALREADY_OPEN"), SemanticId);
        }
        bOpen = true;
    }
    else if (Request.Affordance == EVistaAffordance::Close)
    {
        if (!bOpen)
        {
            return FVistaInteractionResult::Failure(
                EVistaInteractionStatus::InvalidState, TEXT("DOOR_ALREADY_CLOSED"), SemanticId);
        }
        if (IsClosingObstructed())
        {
            return FVistaInteractionResult::Failure(
                EVistaInteractionStatus::Blocked, TEXT("DOOR_SWEEP_OBSTRUCTED"), SemanticId);
        }
        bOpen = false;
    }
    else
    {
        return Super::VistaInteract_Implementation(Request);
    }
    ApplyDoorState(false);
    ForceNetUpdate();
    return FVistaInteractionResult::Success(
        SemanticId, VistaGetRuntimeState_Implementation(),
        bOpen ? TEXT("DOOR_OPENED") : TEXT("DOOR_CLOSED"));
}

void AVistaDoorActor::OnRep_OpenState()
{
    ApplyDoorState(false);
}

void AVistaDoorActor::HandleDoorwayLinkReached(
    UNavLinkCustomComponent* LinkComponent,
    UObject* PathingAgent,
    const FVector& Destination)
{
    UPathFollowingComponent* PathFollowing =
        Cast<UPathFollowingComponent>(PathingAgent);
    if (!IsValid(PathFollowing))
    {
        return;
    }
    if (!HasAuthority())
    {
        // A bound smart-link delegate pauses PathFollowing even on a peer that
        // cannot author movement.  Always release it to avoid a deadlock.
        PathFollowing->FinishUsingCustomLink(LinkComponent);
        return;
    }
    AActor* PathOwner = PathFollowing->GetOwner();
    APawn* MovingPawn = Cast<APawn>(PathOwner);
    if (AController* Controller = Cast<AController>(PathOwner))
    {
        MovingPawn = Controller->GetPawn();
    }
    if (!IsValid(MovingPawn))
    {
        PathFollowing->FinishUsingCustomLink(LinkComponent);
        return;
    }

    const FVector TraversalDestination(
        Destination.X, Destination.Y, MovingPawn->GetActorLocation().Z);
    // Path following has already validated this enabled smart-link edge.
    // Complete the short imported-shell threshold crossing atomically:
    // sweeping here would collide with the same complex wall boundary that
    // the link intentionally bridges and leave the AI paused until timeout.
    MovingPawn->SetActorLocation(
        TraversalDestination, false, nullptr, ETeleportType::TeleportPhysics);
    PathFollowing->FinishUsingCustomLink(LinkComponent);
}

bool AVistaDoorActor::IsClosingObstructed() const
{
    const UStaticMesh* Mesh = DoorMesh->GetStaticMesh();
    const UWorld* World = GetWorld();
    if (!IsValid(Mesh) || !IsValid(World))
    {
        return true;
    }

    const FBox LocalBounds = Mesh->GetBoundingBox();
    const FVector Scale = DoorMesh->GetRelativeScale3D();
    const FVector RawExtent = LocalBounds.GetExtent();
    const FVector Extent(
        FMath::Abs(RawExtent.X * Scale.X) + 2.0f,
        FMath::Abs(RawExtent.Y * Scale.Y) + 2.0f,
        FMath::Abs(RawExtent.Z * Scale.Z) + 2.0f);
    const FVector LeafCenterFromHinge =
        DoorMesh->GetRelativeLocation() + LocalBounds.GetCenter();
    const FTransform RootTransform = SceneRoot->GetComponentTransform();

    FCollisionObjectQueryParams ObjectTypes;
    ObjectTypes.AddObjectTypesToQuery(ECC_Pawn);
    ObjectTypes.AddObjectTypesToQuery(ECC_PhysicsBody);
    ObjectTypes.AddObjectTypesToQuery(ECC_WorldDynamic);
    FCollisionQueryParams QueryParams(SCENE_QUERY_STAT(VistaDoorCloseSweep), false, this);
    QueryParams.AddIgnoredActor(this);

    constexpr int32 SweepSteps = 24;
    for (int32 Step = 0; Step <= SweepSteps; ++Step)
    {
        const float Alpha = static_cast<float>(Step) / static_cast<float>(SweepSteps);
        const FRotator SampleRotation = ClosedRotation +
            FRotator(0.0f, FMath::Lerp(OpenAngleDegrees, 0.0f, Alpha), 0.0f);
        const FVector CenterInRoot = Hinge->GetRelativeLocation() +
            SampleRotation.RotateVector(LeafCenterFromHinge);
        const FVector WorldCenter = RootTransform.TransformPosition(CenterInRoot);
        const FQuat WorldRotation = RootTransform.GetRotation() * SampleRotation.Quaternion();
        TArray<FOverlapResult> Overlaps;
        if (!World->OverlapMultiByObjectType(
                Overlaps, WorldCenter, WorldRotation, ObjectTypes,
                FCollisionShape::MakeBox(Extent), QueryParams))
        {
            continue;
        }
        for (const FOverlapResult& Overlap : Overlaps)
        {
            AActor* Actor = Overlap.GetActor();
            if (!IsValid(Actor) || Actor == this)
            {
                continue;
            }
            if (Actor->IsA<APawn>())
            {
                return true;
            }
            const UPrimitiveComponent* Component = Overlap.GetComponent();
            if (IsValid(Component) && Component->IsSimulatingPhysics())
            {
                return true;
            }
        }
    }
    return false;
}

void AVistaDoorActor::ApplyDoorState(bool bInstant)
{
    TargetRotation = ClosedRotation + FRotator(0.0f, bOpen ? OpenAngleDegrees : 0.0f, 0.0f);
    if (bInstant)
    {
        Hinge->SetRelativeRotation(TargetRotation);
    }
    // Imported leaves have asset-specific pivots and collision hulls.  Once a
    // door is logically open, its leaf must not keep an AI/player capsule
    // blocked at the threshold while the visual sweep finishes.
    DoorMesh->SetCollisionEnabled(
        bOpen ? ECollisionEnabled::NoCollision
              : ECollisionEnabled::QueryAndPhysics);
    DoorwayLink->SetEnabled(bOpen);
    UNavigationSystemV1::UpdateActorInNavOctree(*this);
}
