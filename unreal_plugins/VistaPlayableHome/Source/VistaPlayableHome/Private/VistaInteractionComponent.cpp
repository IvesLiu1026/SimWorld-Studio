#include "VistaInteractionComponent.h"

#include "CollisionQueryParams.h"
#include "Engine/HitResult.h"
#include "Engine/World.h"
#include "GameFramework/Controller.h"
#include "GameFramework/Pawn.h"
#include "VistaInteractable.h"

UVistaInteractionComponent::UVistaInteractionComponent()
{
    PrimaryComponentTick.bCanEverTick = true;
    PrimaryComponentTick.TickInterval = 0.05f;
}

void UVistaInteractionComponent::BeginPlay()
{
    Super::BeginPlay();
    SessionGeneration = 0;
}

void UVistaInteractionComponent::TickComponent(
    float DeltaTime,
    ELevelTick TickType,
    FActorComponentTickFunction* ThisTickFunction)
{
    Super::TickComponent(DeltaTime, TickType, ThisTickFunction);
    UpdateFocus(TraceForInteractable());
}

FString UVistaInteractionComponent::GetFocusedSemanticId() const
{
    AActor* Actor = FocusedActor.Get();
    if (!IsValid(Actor) || !Actor->GetClass()->ImplementsInterface(UVistaInteractable::StaticClass()))
    {
        return FString();
    }
    return IVistaInteractable::Execute_VistaGetSemanticId(Actor);
}

FVistaInteractionResult UVistaInteractionComponent::TryInteract(
    EVistaAffordance Affordance,
    USceneComponent* PlacementAnchor)
{
    AActor* Target = FocusedActor.Get();
    if (!IsValid(Target))
    {
        Target = TraceForInteractable();
        UpdateFocus(Target);
    }
    if (!IsValid(Target) || !Target->GetClass()->ImplementsInterface(UVistaInteractable::StaticClass()))
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::NotFound, TEXT("NO_INTERACTABLE_TARGET"));
    }

    FVistaInteractionRequest Request;
    Request.Affordance = Affordance;
    Request.Requester = GetOwner();
    Request.PlacementAnchor = PlacementAnchor;
    Request.ExpectedRevision = ExpectedRevision;
    Request.SessionGeneration = SessionGeneration;
    return IVistaInteractable::Execute_VistaInteract(Target, Request);
}

AActor* UVistaInteractionComponent::TraceForInteractable() const
{
    const AActor* Owner = GetOwner();
    const APawn* Pawn = Cast<APawn>(Owner);
    if (!IsValid(Owner) || !IsValid(Pawn) || !IsValid(Pawn->GetController()))
    {
        return nullptr;
    }

    FVector ViewLocation;
    FRotator ViewRotation;
    Pawn->GetController()->GetPlayerViewPoint(ViewLocation, ViewRotation);
    const FVector End = ViewLocation + ViewRotation.Vector() * InteractionDistance;

    FHitResult Hit;
    FCollisionQueryParams Params(SCENE_QUERY_STAT(VistaInteraction), false, Owner);
    if (!GetWorld()->LineTraceSingleByChannel(Hit, ViewLocation, End, TraceChannel, Params))
    {
        return nullptr;
    }

    AActor* HitActor = Hit.GetActor();
    return IsValid(HitActor) && HitActor->GetClass()->ImplementsInterface(UVistaInteractable::StaticClass())
        ? HitActor
        : nullptr;
}

void UVistaInteractionComponent::UpdateFocus(AActor* NewFocus)
{
    AActor* Previous = FocusedActor.Get();
    if (Previous == NewFocus)
    {
        return;
    }
    FocusedActor = NewFocus;
    OnFocusChanged.Broadcast(Previous, NewFocus);
}
