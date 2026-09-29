module CodecTests exposing (suite)

{-| The Elm codec has to reproduce the golden command frames in
`../../proto/vectors.json` byte for byte, exactly as the Python one in
`../../custom_components/hlmblue/protocol.py` does, and it has to reject the
torn status frames the unit really sends.
-}

import Codec
import Dict
import Expect
import Random
import Test exposing (Test, describe, test)



-- HELPERS


{-| A frame as lowercase hex, which is how the golden vectors are written.
-}
hex : Codec.Frame -> String
hex =
    List.map byteToHex >> String.concat


byteToHex : Int -> String
byteToHex b =
    String.fromList [ hexDigit (b // 16), hexDigit (modBy 16 b) ]


hexDigit : Int -> Char
hexDigit n =
    if n < 10 then
        Char.fromCode (Char.toCode '0' + n)

    else
        Char.fromCode (Char.toCode 'a' + n - 10)


{-| An ASCII notification, as the notify characteristic delivers it.
-}
notification : String -> List Int
notification =
    String.toList >> List.map Char.toCode



-- TESTS


suite : Test
suite =
    describe "Codec"
        [ goldenFrames
        , realNotifications
        , tornFrames
        , statusFolding
        , passkeys
        ]


goldenFrames : Test
goldenFrames =
    describe "command frames match proto/vectors.json"
        [ vector "power_on" (Codec.setPower True) "ff03eb0001000100000000020000000000000000010000000000"
        , vector "power_off" (Codec.setPower False) "ff03eb0001000100000000020000000000000000010000000001"
        , vector "temp_24" (Codec.setTemp 24) "ff03eb0001000100000000020200000000000000010000000018"
        , vector "temp_16" (Codec.setTemp 16) "ff03eb0001000100000000020200000000000000010000000010"
        , vector "mode_cool" (Codec.setMode "cool") "ff03eb0001000100000000020300000000000000010000000001"
        , vector "mode_auto" (Codec.setMode "auto") "ff03eb0001000100000000020300000000000000010000000002"
        , vector "mode_dry" (Codec.setMode "dry") "ff03eb0001000100000000020300000000000000010000000000"
        , vector "fan_high" (Codec.setFan "high") "ff03eb0001000100000000020100000000000000010000000003"
        , vector "fan_low" (Codec.setFan "low") "ff03eb0001000100000000020100000000000000010000000001"
        , vector "status_request" Codec.statusQuery "ff01f400000001000000000000000000000000000000000000"
        , vector "passkey_4271" (Codec.login "4271") "ff02580004000100000000013400000000000000040000000034323731"
        , test "an unknown mode falls back to cool, as the other codecs do" <|
            \_ -> Expect.equal (hex (Codec.setMode "nonsense")) (hex (Codec.setMode "cool"))
        , test "vertical swing is inverted like power: on is 0" <|
            \_ ->
                Expect.equal ( List.drop 25 (Codec.setSwing True), List.drop 25 (Codec.setSwing False) ) ( [ 0 ], [ 1 ] )
        , test "swing_h carries a two-byte payload" <|
            \_ ->
                Expect.equal (List.drop 25 (Codec.setSwingH True)) [ 0, 1 ]
        , test "an off timer carries u32 BE minutes" <|
            \_ ->
                Expect.equal (List.drop 25 (Codec.setOffTimer 480)) [ 0, 0, 1, 224 ]
        , test "a negative timer clamps to zero rather than wrapping" <|
            \_ ->
                Expect.equal (List.drop 25 (Codec.setOnTimer -5)) [ 0, 0, 0, 0 ]
        , test "every command frame is 25 bytes of header plus its payload" <|
            \_ ->
                Expect.equal (List.length Codec.statusQuery) 25
        ]


vector : String -> Codec.Frame -> String -> Test
vector name frame expected =
    test name <| \_ -> Expect.equal (hex frame) expected


realNotifications : Test
realNotifications =
    describe "notifications captured from HELM__9869"
        [ test "room temperature, slot filled exactly" <|
            \_ ->
                Codec.decodeNotify (notification "Poll:1925:55aa030700086a0200040000001e9f\u{0000}")
                    |> Expect.equal (Just (Dict.fromList [ ( 0x6A, 30 ) ]))
        , test "setpoint, slot filled exactly" <|
            \_ ->
                Codec.decodeNotify (notification "Poll:1934:55aa03070008020200040000001932\u{0000}")
                    |> Expect.equal (Just (Dict.fromList [ ( 0x02, 25 ) ]))
        , test "a short frame, zero-padded out to the 15-byte slot" <|
            \_ ->
                Codec.decodeNotify (notification "Poll:1919:55aa030700051a010001002a000000\u{0000}")
                    |> Expect.equal (Just (Dict.fromList [ ( 0x1A, 0 ) ]))
        , test "Diag lines are not status" <|
            \_ ->
                Codec.decodeNotify (notification "Diag:1171:->100149c4 1")
                    |> Expect.equal Nothing
        , test "anything that is not a Poll line is ignored" <|
            \_ -> Codec.decodeNotify (notification "not a poll") |> Expect.equal Nothing
        ]


tornFrames : Test
tornFrames =
    describe "torn slots are rejected"
        -- Each of these decoded, before the length and checksum were checked, as
        -- the value Home Assistant went on to display. They are the next frame's
        -- header bytes read as a datapoint.
        [ torn "value runs into the next frame's header (was 1437205255 C)"
            "Poll:1926:55aa030700086a02000455aa0307"
        , torn "value half-written, then the next header (was 21930 C)"
            "Poll:1927:55aa0307000802020004000055aa"
        , torn "length field itself overwritten (was 13002344470 C)"
            "Poll:1928:55aa0307000802025 5aa0307000816"
        , torn "hexframe cut mid-byte" "Poll:1929:55aa030700086a020004000"
        , torn "body shorter than the declared length" "Poll:1930:55aa030700086a02"
        , test "a datapoint whose length overruns the body is rejected" <|
            \_ ->
                -- header, length and checksum all agree; the datapoint claims six
                -- value bytes where four remain
                Codec.decodeNotify (notification "Poll:1931:55aa030700086a0200060000001ea1")
                    |> Expect.equal Nothing
        , test "a part-written datapoint header at the tail is rejected" <|
            \_ ->
                Codec.decodeNotify (notification "Poll:1932:55aa0307000701010001006a027f")
                    |> Expect.equal Nothing
        , test "a good checksum with a bad one substituted is rejected" <|
            \_ ->
                Codec.decodeNotify (notification "Poll:1925:55aa030700086a0200040000001e00\u{0000}")
                    |> Expect.equal Nothing
        ]


torn : String -> String -> Test
torn name text =
    test name <|
        \_ ->
            Codec.decodeNotify (notification (String.replace " " "" text))
                |> Expect.equal Nothing


statusFolding : Test
statusFolding =
    let
        start =
            Codec.initialStatus
    in
    describe "toStatus"
        [ test "keeps the previous reading for a datapoint not in the batch" <|
            \_ ->
                Codec.toStatus (Dict.fromList [ ( 0x6A, 27 ) ]) { start | temp = 22 }
                    |> Expect.equal { start | temp = 22, room = 27 }
        , test "reports mode with the status map, not the command map" <|
            \_ ->
                -- status 2 is heat; in a command 2 would mean auto
                (Codec.toStatus (Dict.fromList [ ( 0x04, 2 ) ]) start).mode
                    |> Expect.equal "heat"
        , test "an out-of-range setpoint leaves the last good one alone" <|
            \_ ->
                (Codec.toStatus (Dict.fromList [ ( 0x02, 21930 ) ]) { start | temp = 24 }).temp
                    |> Expect.equal 24
        , test "an out-of-range room temperature leaves the last good one alone" <|
            \_ ->
                (Codec.toStatus (Dict.fromList [ ( 0x6A, 1437205255 ) ]) { start | room = 29 }).room
                    |> Expect.equal 29
        , test "an unknown fan code leaves the last good one alone" <|
            \_ ->
                (Codec.toStatus (Dict.fromList [ ( 0x05, 9 ) ]) { start | fan = "high" }).fan
                    |> Expect.equal "high"
        , test "plausible has no opinion about a DPID with no range" <|
            \_ -> Codec.plausible 0x04 999 |> Expect.equal True
        ]


passkeys : Test
passkeys =
    describe "randomPin"
        [ test "is always four digits and never the 0000 default" <|
            \_ ->
                let
                    pins =
                        List.range 0 200
                            |> List.map (\n -> Tuple.first (Random.step Codec.randomPin (Random.initialSeed n)))
                in
                pins
                    |> List.filter (\p -> String.length p /= 4 || p == "0000")
                    |> Expect.equal []
        , test "a short passkey is left-padded before it goes on the wire" <|
            \_ -> Expect.equal (hex (Codec.login "42")) (hex (Codec.login "0042"))
        ]
