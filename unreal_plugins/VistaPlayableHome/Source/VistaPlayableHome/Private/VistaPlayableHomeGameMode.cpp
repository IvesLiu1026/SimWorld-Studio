#include "VistaPlayableHomeGameMode.h"

#include "EngineUtils.h"
#include "VistaEventSubsystem.h"
#include "VistaEventDefinitionActor.h"
#include "VistaHomeNpcCharacter.h"
#include "VistaInteractionComponent.h"
#include "VistaPlayableHomeCharacter.h"
#include "VistaPlayableHomeHUD.h"
#include "VistaSemanticActor.h"

AVistaPlayableHomeGameMode::AVistaPlayableHomeGameMode()
{
    DefaultPawnClass = AVistaPlayableHomeCharacter::StaticClass();
    HUDClass = AVistaPlayableHomeHUD::StaticClass();
}

void AVistaPlayableHomeGameMode::BeginPlay()
{
    Super::BeginPlay();
    UVistaEventSubsystem* Events = GetWorld()->GetSubsystem<UVistaEventSubsystem>();
    if (IsValid(Events))
    {
        Events->InitializeWorldRevision(WorldRevision);
        TArray<FVistaEventDefinition> CombinedDefinitions = EventDefinitions;
        for (TActorIterator<AVistaEventDefinitionActor> DefinitionActor(GetWorld());
             DefinitionActor; ++DefinitionActor)
        {
            CombinedDefinitions.Append(DefinitionActor->Definitions);
        }
        FName RegistrationCode;
        if (!Events->RegisterEventDefinitions(CombinedDefinitions, RegistrationCode))
        {
            UE_LOG(LogTemp, Error, TEXT("VISTA event registration failed: %s"),
                   *RegistrationCode.ToString());
        }
    }

    for (TActorIterator<AActor> It(GetWorld()); It; ++It)
    {
        if (AVistaSemanticActor* Semantic = Cast<AVistaSemanticActor>(*It))
        {
            Semantic->WorldRevision = WorldRevision;
        }
        if (AVistaHomeNpcCharacter* Npc = Cast<AVistaHomeNpcCharacter>(*It))
        {
            Npc->WorldRevision = WorldRevision;
        }
        if (UVistaInteractionComponent* Interaction =
                It->FindComponentByClass<UVistaInteractionComponent>())
        {
            Interaction->SetExpectedRevision(WorldRevision);
        }
    }
}
