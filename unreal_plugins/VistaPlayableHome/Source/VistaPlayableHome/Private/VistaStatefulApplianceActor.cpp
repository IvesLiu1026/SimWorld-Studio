#include "VistaStatefulApplianceActor.h"

#include "Components/StaticMeshComponent.h"
#include "Net/UnrealNetwork.h"

AVistaStatefulApplianceActor::AVistaStatefulApplianceActor()
{
    Mesh = CreateDefaultSubobject<UStaticMeshComponent>(TEXT("ApplianceMesh"));
    SetRootComponent(Mesh);
    Mesh->SetCollisionProfileName(TEXT("BlockAllDynamic"));
    AllowedAffordances = {EVistaAffordance::Inspect, EVistaAffordance::Toggle};
}

void AVistaStatefulApplianceActor::BeginPlay()
{
    Super::BeginPlay();
    if (HasAuthority())
    {
        bOn = bInitiallyOn;
    }
    OnApplianceStateChanged(bOn);
}

void AVistaStatefulApplianceActor::GetLifetimeReplicatedProps(
    TArray<FLifetimeProperty>& OutLifetimeProps) const
{
    Super::GetLifetimeReplicatedProps(OutLifetimeProps);
    DOREPLIFETIME(AVistaStatefulApplianceActor, bOn);
}

FVistaEntityRuntimeState AVistaStatefulApplianceActor::VistaGetRuntimeState_Implementation() const
{
    FVistaEntityRuntimeState State = Super::VistaGetRuntimeState_Implementation();
    State.Values.Add(TEXT("on"), bOn ? TEXT("true") : TEXT("false"));
    State.Values.Add(TEXT("active"), bOn ? TEXT("true") : TEXT("false"));
    State.Values.Add(TEXT("powered"), bOn ? TEXT("true") : TEXT("false"));
    State.Values.Add(TEXT("appliance_kind"), ApplianceKind.ToString());
    return State;
}

FVistaInteractionResult AVistaStatefulApplianceActor::VistaApplyRuntimeState_Implementation(
    const FVistaEntityRuntimeState& State)
{
    if (!HasAuthority())
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::Rejected, TEXT("AUTHORITY_REQUIRED"), SemanticId);
    }
    const FString* OnValue = State.Values.Find(TEXT("active"));
    if (!OnValue)
    {
        OnValue = State.Values.Find(TEXT("powered"));
    }
    if (!OnValue)
    {
        OnValue = State.Values.Find(TEXT("on"));
    }
    const bool bNewOn = OnValue
        ? OnValue->Equals(TEXT("true"), ESearchCase::CaseSensitive)
        : bOn;
    const FVistaInteractionResult BaseResult = Super::VistaApplyRuntimeState_Implementation(State);
    if (!BaseResult.IsSuccess())
    {
        return BaseResult;
    }
    bOn = bNewOn;
    OnApplianceStateChanged(bOn);
    ForceNetUpdate();
    return FVistaInteractionResult::Success(
        SemanticId, VistaGetRuntimeState_Implementation(), TEXT("APPLIANCE_STATE_APPLIED"));
}

FVistaInteractionResult AVistaStatefulApplianceActor::VistaInteract_Implementation(
    const FVistaInteractionRequest& Request)
{
    const FVistaInteractionResult Validation = ValidateRequest(Request);
    if (!Validation.IsSuccess())
    {
        return Validation;
    }
    if (Request.Affordance != EVistaAffordance::Toggle)
    {
        return Super::VistaInteract_Implementation(Request);
    }
    if (!HasAuthority())
    {
        return FVistaInteractionResult::Failure(
            EVistaInteractionStatus::Rejected, TEXT("AUTHORITY_REQUIRED"), SemanticId);
    }
    bOn = !bOn;
    OnApplianceStateChanged(bOn);
    ForceNetUpdate();
    return FVistaInteractionResult::Success(
        SemanticId, VistaGetRuntimeState_Implementation(),
        bOn ? TEXT("APPLIANCE_ON") : TEXT("APPLIANCE_OFF"));
}

void AVistaStatefulApplianceActor::OnRep_OnState()
{
    OnApplianceStateChanged(bOn);
}
