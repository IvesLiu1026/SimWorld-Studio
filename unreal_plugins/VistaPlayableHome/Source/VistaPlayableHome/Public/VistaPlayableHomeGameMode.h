#pragma once

#include "CoreMinimal.h"
#include "GameFramework/GameModeBase.h"
#include "VistaPlayableHomeTypes.h"
#include "VistaPlayableHomeGameMode.generated.h"

UCLASS(Blueprintable)
class VISTAPLAYABLEHOME_API AVistaPlayableHomeGameMode final : public AGameModeBase
{
    GENERATED_BODY()

public:
    AVistaPlayableHomeGameMode();

    UPROPERTY(EditDefaultsOnly, BlueprintReadOnly, Category = "VISTA|World")
    FName WorldRevision = TEXT("vista_playable_home_r1");

    UPROPERTY(EditDefaultsOnly, BlueprintReadOnly, Category = "VISTA|Events")
    TArray<FVistaEventDefinition> EventDefinitions;

protected:
    virtual void BeginPlay() override;
};
