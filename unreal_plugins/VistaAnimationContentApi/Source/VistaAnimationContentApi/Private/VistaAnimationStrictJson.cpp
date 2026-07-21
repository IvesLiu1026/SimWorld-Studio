#include "VistaAnimationStrictJson.h"

#include "Containers/StringConv.h"
#include "HAL/FileManager.h"
#include "Misc/Char.h"
#include "Misc/Parse.h"
#include "Serialization/Archive.h"

namespace VistaAnimation::StrictJson {
namespace {
constexpr int32 MaxDepth = 16;
constexpr int32 MaxCollectionItems = 128;
constexpr int32 MaxStringCharacters = 1024;

class FParser final {
public:
  explicit FParser(const FString &InText) : Text(InText) {}

  bool Run(TSharedPtr<FValue> &OutValue, FString &OutSafeErrorCode) {
    SkipWhitespace();
    if (!ParseValue(0, OutValue)) {
      OutSafeErrorCode = ErrorCode.IsEmpty() ? TEXT("JSON_INVALID") : ErrorCode;
      return false;
    }
    SkipWhitespace();
    if (Position != Text.Len()) {
      OutSafeErrorCode = TEXT("JSON_TRAILING_DATA");
      return false;
    }
    return true;
  }

private:
  bool ParseValue(int32 Depth, TSharedPtr<FValue> &OutValue) {
    if (Depth > MaxDepth)
      return Fail(TEXT("JSON_DEPTH_EXCEEDED"));
    SkipWhitespace();
    if (Position >= Text.Len())
      return Fail(TEXT("JSON_TRUNCATED"));
    const TCHAR Current = Text[Position];
    if (Current == TEXT('{'))
      return ParseObject(Depth, OutValue);
    if (Current == TEXT('['))
      return ParseArray(Depth, OutValue);
    if (Current == TEXT('"')) {
      FString Value;
      if (!ParseString(Value))
        return false;
      OutValue = MakeShared<FValue>();
      OutValue->Kind = EKind::String;
      OutValue->Scalar = MoveTemp(Value);
      return true;
    }
    if (Current == TEXT('t'))
      return ParseLiteral(TEXT("true"), EKind::Boolean, true, OutValue);
    if (Current == TEXT('f'))
      return ParseLiteral(TEXT("false"), EKind::Boolean, false, OutValue);
    if (Current == TEXT('n'))
      return ParseLiteral(TEXT("null"), EKind::Null, false, OutValue);
    if (Current == TEXT('-') || FChar::IsDigit(Current))
      return ParseNumber(OutValue);
    return Fail(TEXT("JSON_TOKEN_INVALID"));
  }

  bool ParseObject(int32 Depth, TSharedPtr<FValue> &OutValue) {
    ++Position;
    TSharedPtr<FValue> Result = MakeShared<FValue>();
    Result->Kind = EKind::Object;
    SkipWhitespace();
    if (Consume(TEXT('}'))) {
      OutValue = MoveTemp(Result);
      return true;
    }
    while (true) {
      if (Result->Object.Num() >= MaxCollectionItems)
        return Fail(TEXT("JSON_OBJECT_TOO_LARGE"));
      FString Key;
      if (!ParseString(Key))
        return false;
      if (Result->Object.Contains(Key))
        return Fail(TEXT("JSON_DUPLICATE_KEY"));
      SkipWhitespace();
      if (!Consume(TEXT(':')))
        return Fail(TEXT("JSON_OBJECT_INVALID"));
      TSharedPtr<FValue> Child;
      if (!ParseValue(Depth + 1, Child))
        return false;
      Result->Object.Add(MoveTemp(Key), MoveTemp(Child));
      SkipWhitespace();
      if (Consume(TEXT('}')))
        break;
      if (!Consume(TEXT(',')))
        return Fail(TEXT("JSON_OBJECT_INVALID"));
      SkipWhitespace();
    }
    OutValue = MoveTemp(Result);
    return true;
  }

  bool ParseArray(int32 Depth, TSharedPtr<FValue> &OutValue) {
    ++Position;
    TSharedPtr<FValue> Result = MakeShared<FValue>();
    Result->Kind = EKind::Array;
    SkipWhitespace();
    if (Consume(TEXT(']'))) {
      OutValue = MoveTemp(Result);
      return true;
    }
    while (true) {
      if (Result->Array.Num() >= MaxCollectionItems)
        return Fail(TEXT("JSON_ARRAY_TOO_LARGE"));
      TSharedPtr<FValue> Child;
      if (!ParseValue(Depth + 1, Child))
        return false;
      Result->Array.Add(MoveTemp(Child));
      SkipWhitespace();
      if (Consume(TEXT(']')))
        break;
      if (!Consume(TEXT(',')))
        return Fail(TEXT("JSON_ARRAY_INVALID"));
      SkipWhitespace();
    }
    OutValue = MoveTemp(Result);
    return true;
  }

  bool ParseString(FString &Out) {
    SkipWhitespace();
    if (!Consume(TEXT('"')))
      return Fail(TEXT("JSON_STRING_EXPECTED"));
    FString Result;
    while (Position < Text.Len()) {
      const TCHAR Current = Text[Position++];
      if (Current == TEXT('"')) {
        if (Result.Len() > MaxStringCharacters)
          return Fail(TEXT("JSON_STRING_TOO_LARGE"));
        Out = MoveTemp(Result);
        return true;
      }
      if (Current < 0x20 || Current > 0x7f)
        return Fail(TEXT("JSON_STRING_NON_ASCII"));
      if (Current != TEXT('\\')) {
        Result.AppendChar(Current);
        continue;
      }
      if (Position >= Text.Len())
        return Fail(TEXT("JSON_STRING_INVALID_ESCAPE"));
      const TCHAR Escaped = Text[Position++];
      switch (Escaped) {
      case TEXT('"'):
        Result.AppendChar(TEXT('"'));
        break;
      case TEXT('\\'):
        Result.AppendChar(TEXT('\\'));
        break;
      case TEXT('/'):
        Result.AppendChar(TEXT('/'));
        break;
      case TEXT('b'):
        Result.AppendChar(TEXT('\b'));
        break;
      case TEXT('f'):
        Result.AppendChar(TEXT('\f'));
        break;
      case TEXT('n'):
        Result.AppendChar(TEXT('\n'));
        break;
      case TEXT('r'):
        Result.AppendChar(TEXT('\r'));
        break;
      case TEXT('t'):
        Result.AppendChar(TEXT('\t'));
        break;
      case TEXT('u'): {
        if (Position + 4 > Text.Len())
          return Fail(TEXT("JSON_STRING_INVALID_ESCAPE"));
        uint32 CodePoint = 0;
        for (int32 Index = 0; Index < 4; ++Index) {
          const TCHAR Hex = Text[Position++];
          if (!FChar::IsHexDigit(Hex))
            return Fail(TEXT("JSON_STRING_INVALID_ESCAPE"));
          CodePoint =
              (CodePoint << 4) | static_cast<uint32>(FParse::HexDigit(Hex));
        }
        if (CodePoint < 0x20 || CodePoint > 0x7f)
          return Fail(TEXT("JSON_STRING_NON_ASCII"));
        Result.AppendChar(static_cast<TCHAR>(CodePoint));
        break;
      }
      default:
        return Fail(TEXT("JSON_STRING_INVALID_ESCAPE"));
      }
    }
    return Fail(TEXT("JSON_STRING_UNTERMINATED"));
  }

  bool ParseNumber(TSharedPtr<FValue> &OutValue) {
    const int32 Start = Position;
    if (Consume(TEXT('-')) && Position >= Text.Len())
      return Fail(TEXT("JSON_NUMBER_INVALID"));
    if (Consume(TEXT('0'))) {
      if (Position < Text.Len() && FChar::IsDigit(Text[Position]))
        return Fail(TEXT("JSON_NUMBER_INVALID"));
    } else {
      if (Position >= Text.Len() || Text[Position] < TEXT('1') ||
          Text[Position] > TEXT('9'))
        return Fail(TEXT("JSON_NUMBER_INVALID"));
      while (Position < Text.Len() && FChar::IsDigit(Text[Position]))
        ++Position;
    }
    if (Consume(TEXT('.'))) {
      if (Position >= Text.Len() || !FChar::IsDigit(Text[Position]))
        return Fail(TEXT("JSON_NUMBER_INVALID"));
      while (Position < Text.Len() && FChar::IsDigit(Text[Position]))
        ++Position;
    }
    if (Position < Text.Len() &&
        (Text[Position] == TEXT('e') || Text[Position] == TEXT('E'))) {
      ++Position;
      if (Position < Text.Len() &&
          (Text[Position] == TEXT('+') || Text[Position] == TEXT('-')))
        ++Position;
      if (Position >= Text.Len() || !FChar::IsDigit(Text[Position]))
        return Fail(TEXT("JSON_NUMBER_INVALID"));
      while (Position < Text.Len() && FChar::IsDigit(Text[Position]))
        ++Position;
    }
    const FString Lexeme = Text.Mid(Start, Position - Start);
    const double Number = FCString::Atod(*Lexeme);
    if (!FMath::IsFinite(Number))
      return Fail(TEXT("JSON_NUMBER_INVALID"));
    OutValue = MakeShared<FValue>();
    OutValue->Kind = EKind::Number;
    OutValue->Scalar = Lexeme;
    return true;
  }

  bool ParseLiteral(const TCHAR *Literal, EKind Kind, bool Boolean,
                    TSharedPtr<FValue> &OutValue) {
    const int32 Length = FCString::Strlen(Literal);
    if (Text.Mid(Position, Length) != Literal)
      return Fail(TEXT("JSON_LITERAL_INVALID"));
    Position += Length;
    OutValue = MakeShared<FValue>();
    OutValue->Kind = Kind;
    OutValue->Boolean = Boolean;
    return true;
  }

  void SkipWhitespace() {
    while (Position < Text.Len()) {
      const TCHAR Current = Text[Position];
      if (Current != TEXT(' ') && Current != TEXT('\t') &&
          Current != TEXT('\r') && Current != TEXT('\n'))
        break;
      ++Position;
    }
  }

  bool Consume(TCHAR Expected) {
    if (Position >= Text.Len() || Text[Position] != Expected)
      return false;
    ++Position;
    return true;
  }

  bool Fail(const TCHAR *Code) {
    if (ErrorCode.IsEmpty())
      ErrorCode = Code;
    return false;
  }

  const FString &Text;
  int32 Position = 0;
  FString ErrorCode;
};

uint32 RotateRight(uint32 Value, uint32 Amount) {
  return (Value >> Amount) | (Value << (32U - Amount));
}

class FSha256 final {
public:
  FSha256() {
    State[0] = 0x6a09e667U;
    State[1] = 0xbb67ae85U;
    State[2] = 0x3c6ef372U;
    State[3] = 0xa54ff53aU;
    State[4] = 0x510e527fU;
    State[5] = 0x9b05688cU;
    State[6] = 0x1f83d9abU;
    State[7] = 0x5be0cd19U;
  }

  void Update(const uint8 *Data, uint64 Size) {
    TotalBytes += Size;
    while (Size > 0) {
      const uint32 Copy =
          static_cast<uint32>(FMath::Min<uint64>(Size, 64U - BufferSize));
      FMemory::Memcpy(Buffer + BufferSize, Data, Copy);
      BufferSize += Copy;
      Data += Copy;
      Size -= Copy;
      if (BufferSize == 64U) {
        Transform(Buffer);
        BufferSize = 0;
      }
    }
  }

  FString FinalHex() {
    const uint64 BitLength = TotalBytes * 8U;
    Buffer[BufferSize++] = 0x80U;
    if (BufferSize > 56U) {
      while (BufferSize < 64U)
        Buffer[BufferSize++] = 0;
      Transform(Buffer);
      BufferSize = 0;
    }
    while (BufferSize < 56U)
      Buffer[BufferSize++] = 0;
    for (int32 Index = 7; Index >= 0; --Index)
      Buffer[BufferSize++] = static_cast<uint8>(BitLength >> (Index * 8));
    Transform(Buffer);

    FString Result;
    Result.Reserve(64);
    for (uint32 Word : State) {
      Result += FString::Printf(TEXT("%08x"), Word);
    }
    return Result;
  }

private:
  void Transform(const uint8 Block[64]) {
    static constexpr uint32 K[64] = {
        0x428a2f98U, 0x71374491U, 0xb5c0fbcfU, 0xe9b5dba5U, 0x3956c25bU,
        0x59f111f1U, 0x923f82a4U, 0xab1c5ed5U, 0xd807aa98U, 0x12835b01U,
        0x243185beU, 0x550c7dc3U, 0x72be5d74U, 0x80deb1feU, 0x9bdc06a7U,
        0xc19bf174U, 0xe49b69c1U, 0xefbe4786U, 0x0fc19dc6U, 0x240ca1ccU,
        0x2de92c6fU, 0x4a7484aaU, 0x5cb0a9dcU, 0x76f988daU, 0x983e5152U,
        0xa831c66dU, 0xb00327c8U, 0xbf597fc7U, 0xc6e00bf3U, 0xd5a79147U,
        0x06ca6351U, 0x14292967U, 0x27b70a85U, 0x2e1b2138U, 0x4d2c6dfcU,
        0x53380d13U, 0x650a7354U, 0x766a0abbU, 0x81c2c92eU, 0x92722c85U,
        0xa2bfe8a1U, 0xa81a664bU, 0xc24b8b70U, 0xc76c51a3U, 0xd192e819U,
        0xd6990624U, 0xf40e3585U, 0x106aa070U, 0x19a4c116U, 0x1e376c08U,
        0x2748774cU, 0x34b0bcb5U, 0x391c0cb3U, 0x4ed8aa4aU, 0x5b9cca4fU,
        0x682e6ff3U, 0x748f82eeU, 0x78a5636fU, 0x84c87814U, 0x8cc70208U,
        0x90befffaU, 0xa4506cebU, 0xbef9a3f7U, 0xc67178f2U};
    uint32 W[64];
    for (int32 Index = 0; Index < 16; ++Index) {
      W[Index] = (static_cast<uint32>(Block[Index * 4]) << 24) |
                 (static_cast<uint32>(Block[Index * 4 + 1]) << 16) |
                 (static_cast<uint32>(Block[Index * 4 + 2]) << 8) |
                 static_cast<uint32>(Block[Index * 4 + 3]);
    }
    for (int32 Index = 16; Index < 64; ++Index) {
      const uint32 S0 = RotateRight(W[Index - 15], 7) ^
                        RotateRight(W[Index - 15], 18) ^ (W[Index - 15] >> 3);
      const uint32 S1 = RotateRight(W[Index - 2], 17) ^
                        RotateRight(W[Index - 2], 19) ^ (W[Index - 2] >> 10);
      W[Index] = W[Index - 16] + S0 + W[Index - 7] + S1;
    }
    uint32 A = State[0], B = State[1], C = State[2], D = State[3];
    uint32 E = State[4], F = State[5], G = State[6], H = State[7];
    for (int32 Index = 0; Index < 64; ++Index) {
      const uint32 S1 =
          RotateRight(E, 6) ^ RotateRight(E, 11) ^ RotateRight(E, 25);
      const uint32 Choice = (E & F) ^ ((~E) & G);
      const uint32 Temp1 = H + S1 + Choice + K[Index] + W[Index];
      const uint32 S0 =
          RotateRight(A, 2) ^ RotateRight(A, 13) ^ RotateRight(A, 22);
      const uint32 Majority = (A & B) ^ (A & C) ^ (B & C);
      const uint32 Temp2 = S0 + Majority;
      H = G;
      G = F;
      F = E;
      E = D + Temp1;
      D = C;
      C = B;
      B = A;
      A = Temp1 + Temp2;
    }
    State[0] += A;
    State[1] += B;
    State[2] += C;
    State[3] += D;
    State[4] += E;
    State[5] += F;
    State[6] += G;
    State[7] += H;
  }

  uint32 State[8]{};
  uint8 Buffer[64]{};
  uint32 BufferSize = 0;
  uint64 TotalBytes = 0;
};
} // namespace

bool Parse(const FString &Text, TSharedPtr<FValue> &OutValue,
           FString &OutSafeErrorCode) {
  return FParser(Text).Run(OutValue, OutSafeErrorCode);
}

FString Quote(const FString &Value) {
  FString Output = TEXT("\"");
  for (const TCHAR Character : Value) {
    switch (Character) {
    case TEXT('"'):
      Output += TEXT("\\\"");
      break;
    case TEXT('\\'):
      Output += TEXT("\\\\");
      break;
    case TEXT('\b'):
      Output += TEXT("\\b");
      break;
    case TEXT('\f'):
      Output += TEXT("\\f");
      break;
    case TEXT('\n'):
      Output += TEXT("\\n");
      break;
    case TEXT('\r'):
      Output += TEXT("\\r");
      break;
    case TEXT('\t'):
      Output += TEXT("\\t");
      break;
    default:
      if (Character < 0x20)
        Output +=
            FString::Printf(TEXT("\\u%04x"), static_cast<uint32>(Character));
      else
        Output.AppendChar(Character);
      break;
    }
  }
  Output += TEXT("\"");
  return Output;
}

FString Canonicalize(const FValue &Value) {
  switch (Value.Kind) {
  case EKind::Null:
    return TEXT("null");
  case EKind::Boolean:
    return Value.Boolean ? TEXT("true") : TEXT("false");
  case EKind::Number:
    return Value.Scalar;
  case EKind::String:
    return Quote(Value.Scalar);
  case EKind::Array: {
    TArray<FString> Items;
    Items.Reserve(Value.Array.Num());
    for (const TSharedPtr<FValue> &Item : Value.Array)
      Items.Add(Canonicalize(*Item));
    return FString::Printf(TEXT("[%s]"), *FString::Join(Items, TEXT(",")));
  }
  case EKind::Object: {
    TArray<FString> Keys;
    Value.Object.GetKeys(Keys);
    Keys.Sort();
    TArray<FString> Fields;
    Fields.Reserve(Keys.Num());
    for (const FString &Key : Keys) {
      Fields.Add(Quote(Key) + TEXT(":") +
                 Canonicalize(*Value.Object.FindChecked(Key)));
    }
    return FString::Printf(TEXT("{%s}"), *FString::Join(Fields, TEXT(",")));
  }
  }
  return TEXT("null");
}

bool ExactKeys(const FValue &Value,
               std::initializer_list<const TCHAR *> Expected,
               FString &OutSafeErrorCode) {
  if (Value.Kind != EKind::Object) {
    OutSafeErrorCode = TEXT("JSON_OBJECT_EXPECTED");
    return false;
  }
  if (Value.Object.Num() != static_cast<int32>(Expected.size())) {
    OutSafeErrorCode = TEXT("JSON_SHAPE_INVALID");
    return false;
  }
  for (const TCHAR *Key : Expected) {
    if (!Value.Object.Contains(Key)) {
      OutSafeErrorCode = TEXT("JSON_SHAPE_INVALID");
      return false;
    }
  }
  return true;
}

const FValue *Field(const FValue &Value, const TCHAR *Name) {
  if (Value.Kind != EKind::Object)
    return nullptr;
  const TSharedPtr<FValue> *Found = Value.Object.Find(Name);
  return Found ? Found->Get() : nullptr;
}

bool ReadString(const FValue &Value, FString &Out) {
  if (Value.Kind != EKind::String)
    return false;
  Out = Value.Scalar;
  return true;
}

bool ReadBoolean(const FValue &Value, bool &Out) {
  if (Value.Kind != EKind::Boolean)
    return false;
  Out = Value.Boolean;
  return true;
}

bool ReadNumber(const FValue &Value, double &Out) {
  if (Value.Kind != EKind::Number)
    return false;
  Out = FCString::Atod(*Value.Scalar);
  return FMath::IsFinite(Out);
}

bool ReadNullableString(const FValue &Value, TOptional<FString> &Out) {
  if (Value.Kind == EKind::Null) {
    Out.Reset();
    return true;
  }
  FString String;
  if (!ReadString(Value, String))
    return false;
  Out = MoveTemp(String);
  return true;
}

FString Sha256HexUtf8(const FString &Text) {
  FTCHARToUTF8 Utf8(*Text);
  FSha256 Hash;
  Hash.Update(reinterpret_cast<const uint8 *>(Utf8.Get()),
              static_cast<uint64>(Utf8.Length()));
  return Hash.FinalHex();
}

bool Sha256File(const FString &Filename, FString &OutHex) {
  TUniquePtr<FArchive> Reader(IFileManager::Get().CreateFileReader(*Filename));
  if (!Reader)
    return false;
  FSha256 Hash;
  uint8 Buffer[64 * 1024];
  while (!Reader->AtEnd() && !Reader->IsError()) {
    const int64 Remaining = Reader->TotalSize() - Reader->Tell();
    const int64 Count = FMath::Min<int64>(Remaining, UE_ARRAY_COUNT(Buffer));
    if (Count <= 0)
      break;
    Reader->Serialize(Buffer, Count);
    if (Reader->IsError())
      return false;
    Hash.Update(Buffer, static_cast<uint64>(Count));
  }
  if (Reader->IsError())
    return false;
  OutHex = Hash.FinalHex();
  return true;
}
} // namespace VistaAnimation::StrictJson
