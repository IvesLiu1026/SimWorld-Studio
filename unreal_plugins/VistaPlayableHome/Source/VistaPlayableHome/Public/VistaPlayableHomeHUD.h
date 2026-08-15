#pragma once

#include "CoreMinimal.h"
#include "GameFramework/HUD.h"
#include "VistaPlayableHomeHUD.generated.h"

UCLASS()
class VISTAPLAYABLEHOME_API AVistaPlayableHomeHUD final : public AHUD
{
    GENERATED_BODY()

public:
    virtual void DrawHUD() override;
};
