#pragma once

#include "CoreMinimal.h"
#include "UObject/Interface.h"
#include "VistaItemCarrier.generated.h"

class USceneComponent;

UINTERFACE(BlueprintType)
class VISTAPLAYABLEHOME_API UVistaItemCarrier : public UInterface
{
    GENERATED_BODY()
};

class VISTAPLAYABLEHOME_API IVistaItemCarrier
{
    GENERATED_BODY()

public:
    UFUNCTION(BlueprintNativeEvent, BlueprintCallable, Category = "VISTA|Carry")
    USceneComponent* VistaGetCarryAnchor() const;

    UFUNCTION(BlueprintNativeEvent, BlueprintCallable, Category = "VISTA|Carry")
    AActor* VistaGetHeldItem() const;

    UFUNCTION(BlueprintNativeEvent, BlueprintCallable, Category = "VISTA|Carry")
    bool VistaTryClaimItem(AActor* Item);

    UFUNCTION(BlueprintNativeEvent, BlueprintCallable, Category = "VISTA|Carry")
    void VistaReleaseItem(AActor* Item);
};
