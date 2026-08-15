#include "VistaEventDefinitionActor.h"

AVistaEventDefinitionActor::AVistaEventDefinitionActor()
{
    PrimaryActorTick.bCanEverTick = false;
    bReplicates = false;
    SetActorHiddenInGame(true);
}
