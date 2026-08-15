#include "VistaContainerActor.h"

#include "Components/StaticMeshComponent.h"
#include "Net/UnrealNetwork.h"

AVistaContainerActor::AVistaContainerActor()
{
    Mesh = CreateDefaultSubobject<UStaticMeshComponent>(TEXT("ContainerMesh"));
    SetRootComponent(Mesh);
    Mesh->SetCollisionProfileName(TEXT("BlockAllDynamic"));
    AllowedAffordances = {
        EVistaAffordance::Inspect,
        EVistaAffordance::Open,
        EVistaAffordance::Close};
}

void AVistaContainerActor::BeginPlay()
{
    Super::BeginPlay();
    if (HasAuthority())
    {
        bOpen = bInitiallyOpen;
    }
    OnContainerStateChanged(bOpen);
}

void AVistaContainerActor::GetLifetimeReplicatedProps(
    TArray<FLifetimeProperty>& OutLifetimeProps) const
{
    Super::GetLifetimeReplicatedProps(OutLifetimeProps);
    DOREPLIFETIME(AVistaContainerActor, bOpen);
}

FVistaEntityRuntimeState AVistaContainerActor::VistaGetRuntimeState_Implementation() const
{
    FVistaEntityRuntimeState State = Super::VistaGetRuntimeState_Implementation();
    State.Values.Add(TEXT("open"), bOpen ? TEXT("true") : TEXT("false"));
    return State;
}

FVistaInteractionResult AVistaContainerActor::VistaApplyRuntimeState_Implementation(
    const FVistaEntityRuntimeState& State)
{
    if (!HasAuthority())
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::Rejected, TEXT("AUTHORITY_REQUIRED"), SemanticId);
    }
    const FVistaInteractionResult BaseResult = Super::VistaApplyRuntimeState_Implementation(State);
    if (!BaseResult.IsSuccess())
    {
        return BaseResult;
    }
    const FString* Open = State.Values.Find(TEXT("open"));
    if (Open)
    {
        bOpen = Open->Equals(TEXT("true"), ESearchCase::CaseSensitive);
    }
    OnContainerStateChanged(bOpen);
    ForceNetUpdate();
    return FVistaInteractionResult::Success(
        SemanticId, VistaGetRuntimeState_Implementation(), TEXT("CONTAINER_STATE_APPLIED"));
}

FVistaInteractionResult AVistaContainerActor::VistaInteract_Implementation(
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
    if (Request.Affordance != EVistaAffordance::Open &&
        Request.Affordance != EVistaAffordance::Close)
    {
        return Super::VistaInteract_Implementation(Request);
    }
    const bool bRequestedOpen = Request.Affordance == EVistaAffordance::Open;
    if (bRequestedOpen == bOpen)
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::InvalidState,
            bOpen ? FName(TEXT("CONTAINER_ALREADY_OPEN"))
                  : FName(TEXT("CONTAINER_ALREADY_CLOSED")),
            SemanticId);
    }
    bOpen = bRequestedOpen;
    OnContainerStateChanged(bOpen);
    ForceNetUpdate();
    return FVistaInteractionResult::Success(
        SemanticId, VistaGetRuntimeState_Implementation(),
        bOpen ? TEXT("CONTAINER_OPENED") : TEXT("CONTAINER_CLOSED"));
}

void AVistaContainerActor::OnRep_OpenState()
{
    OnContainerStateChanged(bOpen);
}
