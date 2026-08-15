#include "VistaPlayableHomeHUD.h"

#include "Engine/Canvas.h"
#include "Engine/Engine.h"
#include "Engine/Font.h"
#include "VistaEventSubsystem.h"
#include "VistaInteractionComponent.h"
#include "VistaPickupActor.h"
#include "VistaPlayableHomeCharacter.h"

void AVistaPlayableHomeHUD::DrawHUD()
{
    Super::DrawHUD();
    if (!Canvas || !PlayerOwner || !GEngine)
    {
        return;
    }
    const AVistaPlayableHomeCharacter* Character =
        Cast<AVistaPlayableHomeCharacter>(PlayerOwner->GetPawn());
    if (!IsValid(Character))
    {
        return;
    }

    UFont* Font = GEngine->GetSmallFont();
    const FLinearColor Primary(0.86f, 0.90f, 0.96f, 1.0f);
    const FLinearColor Muted(0.55f, 0.61f, 0.70f, 1.0f);
    float Y = Canvas->ClipY - 86.0f;

    if (const UVistaInteractionComponent* Interaction = Character->InteractionComponent)
    {
        const FString FocusedId = Interaction->GetFocusedSemanticId();
        if (!FocusedId.IsEmpty())
        {
            DrawText(FString::Printf(TEXT("E  Interact  |  %s"), *FocusedId),
                     Primary, 32.0f, Y, Font, 1.0f, false);
            Y += 18.0f;
        }
    }

    const AVistaPickupActor* Held = Character->GetHeldPickup();
    DrawText(IsValid(Held)
                 ? FString::Printf(TEXT("Held: %s  |  Q  Drop"), *Held->SemanticId)
                 : TEXT("Held: none"),
             Muted, 32.0f, Y, Font, 1.0f, false);
    Y += 18.0f;

    const UVistaEventSubsystem* Events = GetWorld()->GetSubsystem<UVistaEventSubsystem>();
    if (IsValid(Events) && !Events->GetActiveEventId().IsNone())
    {
        DrawText(FString::Printf(TEXT("Event: %s  |  %s"),
                                *Events->GetActiveEventId().ToString(),
                                *Events->GetPublicGoal()),
                 Primary, 32.0f, Y, Font, 1.0f, false);
    }
}
