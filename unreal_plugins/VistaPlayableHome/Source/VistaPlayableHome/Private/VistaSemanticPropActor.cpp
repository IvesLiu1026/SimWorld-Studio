#include "VistaSemanticPropActor.h"

#include "Components/StaticMeshComponent.h"

AVistaSemanticPropActor::AVistaSemanticPropActor()
{
    Mesh = CreateDefaultSubobject<UStaticMeshComponent>(TEXT("PropMesh"));
    SetRootComponent(Mesh);
    Mesh->SetCollisionProfileName(TEXT("BlockAllDynamic"));
    AllowedAffordances = {EVistaAffordance::Inspect};
}
