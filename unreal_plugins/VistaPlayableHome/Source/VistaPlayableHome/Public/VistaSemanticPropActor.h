#pragma once

#include "CoreMinimal.h"
#include "VistaSemanticActor.h"
#include "VistaSemanticPropActor.generated.h"

class UStaticMeshComponent;

/** Stateful semantic prop used when no narrower gameplay class is required. */
UCLASS(Blueprintable)
class VISTAPLAYABLEHOME_API AVistaSemanticPropActor final : public AVistaSemanticActor
{
    GENERATED_BODY()

public:
    AVistaSemanticPropActor();

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "VISTA|Prop")
    TObjectPtr<UStaticMeshComponent> Mesh;
};
