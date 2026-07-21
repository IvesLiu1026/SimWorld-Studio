#pragma once

#include "CoreMinimal.h"

namespace VistaAnimation::StrictJson {
enum class EKind : uint8 { Null, Boolean, Number, String, Array, Object };

struct FValue final : public TSharedFromThis<FValue> {
  EKind Kind = EKind::Null;
  bool Boolean = false;
  FString Scalar;
  TArray<TSharedPtr<FValue>> Array;
  TMap<FString, TSharedPtr<FValue>> Object;
};

bool Parse(const FString &Text, TSharedPtr<FValue> &OutValue,
           FString &OutSafeErrorCode);
FString Canonicalize(const FValue &Value);
FString Quote(const FString &Value);

bool ExactKeys(const FValue &Value,
               std::initializer_list<const TCHAR *> Expected,
               FString &OutSafeErrorCode);
const FValue *Field(const FValue &Value, const TCHAR *Name);
bool ReadString(const FValue &Value, FString &Out);
bool ReadBoolean(const FValue &Value, bool &Out);
bool ReadNumber(const FValue &Value, double &Out);
bool ReadNullableString(const FValue &Value, TOptional<FString> &Out);

FString Sha256HexUtf8(const FString &Text);
bool Sha256File(const FString &Filename, FString &OutHex);
} // namespace VistaAnimation::StrictJson
