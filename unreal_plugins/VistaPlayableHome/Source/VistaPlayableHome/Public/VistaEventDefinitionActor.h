#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "VistaPlayableHomeTypes.h"
#include "VistaEventDefinitionActor.generated.h"

/** Data-only map actor authored by the deterministic composition commandlet. */
UCLASS(NotBlueprintable)
class VISTAPLAYABLEHOME_API AVistaEventDefinitionActor final : public AActor
{
    GENERATED_BODY()

public:
    AVistaEventDefinitionActor();

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "VISTA|Events")
    TArray<FVistaEventDefinition> Definitions;
};
