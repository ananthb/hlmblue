module Codec exposing
    ( Status, initialStatus
    , Frame, statusQuery, login, setPower, setTemp, setMode, setFan
    , setSwing, setSwingH, setOffTimer, setOnTimer
    , decodeNotify, toStatus, plausible
    , modes, fans, tempMin, tempMax, randomPin
    , dpPower, runningWatts
    )

{-| The A/C wire codec, pure Elm.

Commands are a 25-byte header plus a payload, written to the command
characteristic. Status arrives on the notify characteristic as ASCII text
`Poll:<seq>:<hexframe>`, where `<hexframe>` is a Tuya datapoint frame padded
with `00` to a fixed 15-byte slot.

The unit tears that slot — a notification can carry a part-written frame with
the start of the next one behind it — so `decodeNotify` verifies the declared
body length and the checksum before it believes a datapoint. Without that, the
next frame's header bytes decode as a reading: `0x55aa` as a 21930 °C setpoint
was the symptom.

See `../../docs/protocol.md` for the wire format, `../../proto/vectors.json`
for the golden command frames, and `../tests/CodecTests.elm` for both.

@docs Status, initialStatus
@docs Frame, statusQuery, login, setPower, setTemp, setMode, setFan
@docs setSwing, setSwingH, setOffTimer, setOnTimer
@docs decodeNotify, toStatus, plausible
@docs modes, fans, tempMin, tempMax, randomPin
@docs dpPower, runningWatts

-}

import Array exposing (Array)
import Bitwise
import Dict exposing (Dict)
import Random



-- TYPES


{-| A frame as it goes on the wire: one byte per element, each 0..255.
-}
type alias Frame =
    List Int


{-| The folded state of the unit, as the UI wants it.
-}
type alias Status =
    { temp : Int
    , mode : String
    , fan : String
    , room : Int
    , watts : Int
    }


initialStatus : Status
initialStatus =
    { temp = 24, mode = "cool", fan = "auto", room = 0, watts = 0 }



-- CONSTANTS


{-| command\_id
-}
cmdStatusData : Int
cmdStatusData =
    500


cmdBlePasskey : Int
cmdBlePasskey =
    600


cmdAcCtrl : Int
cmdAcCtrl =
    1003


{-| AC command index, carried in level[0] of an AC\_CTRL frame.
-}
acPower : Int
acPower =
    0


acSpeed : Int
acSpeed =
    1


acTemp : Int
acTemp =
    2


acMode : Int
acMode =
    3


acSwing : Int
acSwing =
    4


acOffTimer : Int
acOffTimer =
    18


acOnTimer : Int
acOnTimer =
    19


acSwingH : Int
acSwingH =
    21


{-| ON is 0, OFF is 1. Not a typo in the firmware's favour.
-}
powerOn : Int
powerOn =
    0


powerOff : Int
powerOff =
    1


{-| Above this power draw the compressor is working rather than just the fan.
Measured on a live unit: 19-24 W idle or on the fan alone, 61-91 W while the fan
runs on after a power-off, and 316 W upwards once the inverter compressor is
doing something.
-}
runningWatts : Int
runningWatts =
    150


tempMin : Int
tempMin =
    16


tempMax : Int
tempMax =
    30


{-| The modes and fan speeds the UI cycles through, in cycle order.
-}
modes : List String
modes =
    [ "cool", "dry", "fan", "auto", "heat" ]


fans : List String
fans =
    [ "auto", "low", "medium", "high" ]


{-| Mode as the unit takes it in a command.
-}
modeCode : String -> Int
modeCode m =
    case m of
        "dry" ->
            0

        "cool" ->
            1

        "auto" ->
            2

        "fan" ->
            3

        "heat" ->
            4

        "wind" ->
            5

        "wet" ->
            6

        _ ->
            1


{-| Mode as the unit _reports_ it, which is a different map from the one above
(verified live): status 0=auto, 1=cool, 2=heat, 3=dry, 4=fan.
-}
statusMode : Int -> Maybe String
statusMode n =
    case n of
        0 ->
            Just "auto"

        1 ->
            Just "cool"

        2 ->
            Just "heat"

        3 ->
            Just "dry"

        4 ->
            Just "fan"

        _ ->
            Nothing


fanCode : String -> Int
fanCode f =
    case f of
        "auto" ->
            0

        "low" ->
            1

        "medium" ->
            2

        "high" ->
            3

        _ ->
            0


fanName : Int -> Maybe String
fanName n =
    case n of
        0 ->
            Just "auto"

        1 ->
            Just "low"

        2 ->
            Just "medium"

        3 ->
            Just "high"

        _ ->
            Nothing


{-| Status DPIDs.
-}
dpPower : Int
dpPower =
    0x01


dpTemp : Int
dpTemp =
    0x02


dpMode : Int
dpMode =
    0x04


dpFan : Int
dpFan =
    0x05


dpPowerW : Int
dpPowerW =
    0x1C


dpRoomTemp : Int
dpRoomTemp =
    0x6A



-- ENCODING


u16 : Int -> List Int
u16 n =
    [ Bitwise.and (Bitwise.shiftRightBy 8 n) 0xFF, Bitwise.and n 0xFF ]


u32 : Int -> List Int
u32 n =
    [ Bitwise.and (Bitwise.shiftRightBy 24 n) 0xFF
    , Bitwise.and (Bitwise.shiftRightBy 16 n) 0xFF
    , Bitwise.and (Bitwise.shiftRightBy 8 n) 0xFF
    , Bitwise.and n 0xFF
    ]


{-| CommandPacketBuilder.serialize(): a 25-byte header, then the payload.

      off  size  field        notes
       0    1    header       always 0xFF
       1    2    cmdId        u16 BE
       3    2    len          u16 BE, payload length
       5    2    seqNum       u16 BE, always 1
       7    4    checksum     u32 BE, 0 (the unit does not validate it)
      11    1    total_level
      12    5    level        level[0] carries the command; level[1..4] are 0
      17    4    totalSize    u32 BE, = len
      21    4    params       u32 BE, 0

-}
serialize : Int -> Int -> Int -> List Int -> Frame
serialize cmdId totalLevel level0 payload =
    let
        len =
            List.length payload
    in
    List.concat
        [ [ 0xFF ]
        , u16 cmdId
        , u16 len
        , u16 1
        , u32 0
        , [ totalLevel, level0, 0, 0, 0, 0 ]
        , u32 len
        , u32 0
        , payload
        ]


{-| An AC\_CTRL frame with a one-byte payload.
-}
acFrame : Int -> Int -> Frame
acFrame command value =
    serialize cmdAcCtrl 2 command [ Bitwise.and value 0xFF ]


{-| An AC\_CTRL frame with a payload the caller has already laid out.
-}
acBytes : Int -> List Int -> Frame
acBytes command payload =
    serialize cmdAcCtrl 2 command payload


statusQuery : Frame
statusQuery =
    serialize cmdStatusData 0 0 []


{-| Unlock the unit. The passkey is four ASCII digits, and level[0] repeats the
first of them, as the vendor app sends it.
-}
login : String -> Frame
login pin =
    let
        digits =
            String.padLeft 4 '0' pin |> String.left 4 |> String.toList |> List.map Char.toCode
    in
    serialize cmdBlePasskey 1 (List.head digits |> Maybe.withDefault 0x30) digits


setPower : Bool -> Frame
setPower on =
    acFrame acPower
        (if on then
            powerOn

         else
            powerOff
        )


setTemp : Int -> Frame
setTemp celsius =
    acFrame acTemp celsius


setMode : String -> Frame
setMode mode =
    acFrame acMode (modeCode mode)


setFan : String -> Frame
setFan fan =
    acFrame acSpeed (fanCode fan)


{-| Vertical swing. Inverted like power: on is 0 (verified live).
-}
setSwing : Bool -> Frame
setSwing on =
    acFrame acSwing (boolBit (not on))


{-| Horizontal swing, whose payload is two bytes rather than one.
-}
setSwingH : Bool -> Frame
setSwingH on =
    acBytes acSwingH [ 0, boolBit on ]


{-| Auto-off after `minutes` (0 cancels). The payload is u32 BE minutes.
-}
setOffTimer : Int -> Frame
setOffTimer minutes =
    acBytes acOffTimer (u32 (max 0 minutes))


{-| Auto-on after `minutes` (0 cancels).
-}
setOnTimer : Int -> Frame
setOnTimer minutes =
    acBytes acOnTimer (u32 (max 0 minutes))


boolBit : Bool -> Int
boolBit on =
    if on then
        1

    else
        0


{-| A random 4-digit passkey, never 0000 — that is the factory default and the
reset value, so it is never handed out as a new one.
-}
randomPin : Random.Generator String
randomPin =
    Random.int 1 9999
        |> Random.map (String.fromInt >> String.padLeft 4 '0')



-- DECODING


{-| Decode one notification into its datapoints, keyed by DPID.

`Nothing` for anything that is not a verified `Poll` frame: a `Diag` line, a
hexframe that does not parse, a length that overruns the data, a checksum that
does not add up, or a datapoint that runs past the end of the body. Those last
three are how a torn slot is caught.

-}
decodeNotify : List Int -> Maybe (Dict Int Int)
decodeNotify raw =
    let
        text =
            raw |> List.map Char.fromCode |> String.fromList |> dropTrailingNuls

        parts =
            String.split ":" text
    in
    case ( List.head parts, lastOf parts ) of
        ( Just "Poll", Just hex ) ->
            if List.length parts < 3 then
                Nothing

            else
                hexToBytes hex |> Maybe.andThen verifiedDatapoints

        _ ->
            Nothing


{-| Walk a verified frame's body. `end` is the index of the checksum byte, so
the body is `[6, end)` and the padding beyond the checksum is never read.
-}
verifiedDatapoints : Array Int -> Maybe (Dict Int Int)
verifiedDatapoints b =
    let
        at i =
            Array.get i b |> Maybe.withDefault 0

        end =
            6 + (Bitwise.shiftLeftBy 8 (at 4) + at 5)
    in
    if Array.length b < 7 || at 0 /= 0x55 || at 1 /= 0xAA then
        Nothing

    else if Array.length b <= end || checksum b end /= at end then
        Nothing

    else
        walk b end 6 Dict.empty


checksum : Array Int -> Int -> Int
checksum b end =
    Array.slice 0 end b |> Array.foldl (+) 0 |> Bitwise.and 0xFF


walk : Array Int -> Int -> Int -> Dict Int Int -> Maybe (Dict Int Int)
walk b end i acc =
    if i >= end then
        Just acc

    else if i + 4 > end then
        -- A part-written datapoint header at the tail: the frame is torn.
        Nothing

    else
        let
            at k =
                Array.get k b |> Maybe.withDefault 0

            dlen =
                Bitwise.shiftLeftBy 8 (at (i + 2)) + at (i + 3)
        in
        if i + 4 + dlen > end then
            Nothing

        else
            walk b end (i + 4 + dlen) (Dict.insert (at i) (beInt b (i + 4) dlen) acc)


{-| The `dlen` value bytes at `from`, big-endian. An empty value reads as 0.
-}
beInt : Array Int -> Int -> Int -> Int
beInt b from dlen =
    Array.slice from (from + dlen) b |> Array.foldl (\byte n -> n * 256 + byte) 0


{-| Fold a batch of datapoints into the status the UI shows, keeping the
previous reading for anything the batch does not mention.

Two things are deliberately not folded in. Swing, because the unit's read-back
(DPIDs 0x6e/0x6f) does not map cleanly onto vertical and horizontal, so the UI
tracks what it last set instead. And on/off, because no report says whether the
unit is on — see `Power`.

-}
toStatus : Dict Int Int -> Status -> Status
toStatus dp prev =
    let
        believed key =
            Dict.get key dp |> Maybe.andThen (keepIfPlausible key)
    in
    { temp = believed dpTemp |> Maybe.withDefault prev.temp
    , mode = believed dpMode |> Maybe.andThen statusMode |> Maybe.withDefault prev.mode
    , fan = believed dpFan |> Maybe.andThen fanName |> Maybe.withDefault prev.fan
    , room = believed dpRoomTemp |> Maybe.withDefault prev.room
    , watts = believed dpPowerW |> Maybe.withDefault prev.watts
    }


keepIfPlausible : Int -> Int -> Maybe Int
keepIfPlausible dpid value =
    if plausible dpid value then
        Just value

    else
        Nothing


{-| Is `value` in range for `dpid`? True for a DPID with no range.

A torn frame whose checksum happens to land is still nonsense, so a reading
outside the unit's own range is dropped and the last good one kept.

-}
plausible : Int -> Int -> Bool
plausible dpid value =
    if dpid == dpTemp then
        value >= tempMin && value <= tempMax

    else if dpid == dpRoomTemp then
        value >= -20 && value <= 70

    else if dpid == dpPowerW then
        value >= 0 && value <= 20000

    else
        True



-- HELPERS


dropTrailingNuls : String -> String
dropTrailingNuls s =
    if String.endsWith "\u{0000}" s then
        dropTrailingNuls (String.dropRight 1 s)

    else
        s


lastOf : List a -> Maybe a
lastOf =
    List.reverse >> List.head


{-| Parse an even-length hex string. `Nothing` if it is ragged or not hex,
which is how a slot torn mid-byte is rejected.
-}
hexToBytes : String -> Maybe (Array Int)
hexToBytes hex =
    let
        chars =
            String.toList hex
    in
    if modBy 2 (List.length chars) /= 0 then
        Nothing

    else
        pairUp chars [] |> Maybe.map Array.fromList


pairUp : List Char -> List Int -> Maybe (List Int)
pairUp chars acc =
    case chars of
        [] ->
            Just (List.reverse acc)

        hi :: lo :: rest ->
            case ( hexDigit hi, hexDigit lo ) of
                ( Just h, Just l ) ->
                    pairUp rest (h * 16 + l :: acc)

                _ ->
                    Nothing

        _ ->
            Nothing


hexDigit : Char -> Maybe Int
hexDigit c =
    let
        n =
            Char.toCode (Char.toLower c)
    in
    if n >= 0x30 && n <= 0x39 then
        Just (n - 0x30)

    else if n >= 0x61 && n <= 0x66 then
        Just (n - 0x61 + 10)

    else
        Nothing
