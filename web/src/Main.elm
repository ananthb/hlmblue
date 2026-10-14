port module Main exposing (main)

{-| A Web Bluetooth remote for broadcast BLE air conditioners, styled as an LCD remote.

Elm owns the UI, the connection state machine, the wire codec (`Codec`) and the
unit's on/off state (`Power`), all pure; plain JS (js/ble.js) owns only the Web
Bluetooth transport and the persisted device registry. Frames go out as bytes on
`sendIntent`; raw notifications and transport events come back on `bleEvents`.

-}

import Browser
import Codec
import Html exposing (..)
import Html.Attributes exposing (..)
import Html.Events exposing (onClick)
import Json.Decode as D
import Json.Encode as E
import Power
import Random
import Time



-- PORTS


port sendIntent : E.Value -> Cmd msg


port bleEvents : (D.Value -> msg) -> Sub msg



-- MODEL


type Conn
    = Disconnected
    | Connecting
    | NeedPin
    | Ready


type alias Dev =
    { id : String, name : String, pin : String, available : Bool }


type alias Model =
    { conn : Conn
    , status : Codec.Status
    , power : Power.Power

    -- A plain count of Tick seconds. Only differences matter, and it is what
    -- Power measures its settle window in.
    , clock : Int
    , timerMin : Int
    , swingV : Bool
    , swingH : Bool
    , device : Maybe String
    , currentId : Maybe String
    , devices : List Dev
    , menuOpen : Bool
    , reveal : Maybe String
    , error : Maybe String
    , backlight : Bool
    , idle : Int
    }


backlightTimeout : Int
backlightTimeout =
    60


init : () -> ( Model, Cmd Msg )
init _ =
    ( { conn = Disconnected
      , status = Codec.initialStatus
      , power = Power.initial
      , clock = 0
      , timerMin = 0
      , swingV = False
      , swingH = False
      , device = Nothing
      , currentId = Nothing
      , devices = []
      , menuOpen = False
      , reveal = Nothing
      , error = Nothing
      , backlight = True
      , idle = 0
      }
      -- The status query is a constant frame, so hand it over once and let the
      -- transport re-send it as its own poll.
    , Cmd.batch
        [ intent [ ( "kind", E.string "listDevices" ) ]
        , intent
            [ ( "kind", E.string "pollFrame" )
            , ( "frames", encodeFrames [ Codec.statusQuery ] )
            ]
        ]
    )



-- UPDATE


type Msg
    = AddDevice
    | PickDevice String
    | RemoveDevice String
    | RevealPin String
    | NewPasskey String
    | ToggleMenu
    | Disconnect
    | SetPower Bool
    | SetTemp Int
    | CycleMode
    | CycleFan
    | SetSwing Bool
    | SetSwingH Bool
    | CycleTimer
    | PasskeyGenerated String String
    | Event D.Value
    | Tick


intent : List ( String, E.Value ) -> Cmd Msg
intent fields =
    sendIntent (E.object fields)


encodeFrames : List Codec.Frame -> E.Value
encodeFrames =
    E.list (E.list E.int)


{-| Write frames to the unit, in order. `poll` asks the transport to follow them
with a status query once the unit has had a moment to act on them.
-}
write : Bool -> List Codec.Frame -> Cmd Msg
write poll frames =
    intent
        [ ( "kind", E.string "write" )
        , ( "frames", encodeFrames frames )
        , ( "poll", E.bool poll )
        ]


{-| Unlock the unit with `pin`, then ask what state it is in. This pair is sent
on connecting and after every passkey change, which is how the unit is told a
new passkey in the first place.
-}
unlock : String -> List Codec.Frame
unlock pin =
    [ Codec.login pin, Codec.statusQuery ]


next : List String -> String -> String
next xs cur =
    case xs of
        [] ->
            cur

        first :: _ ->
            let
                go list =
                    case list of
                        a :: b :: rest ->
                            if a == cur then
                                b

                            else
                                go (b :: rest)

                        _ ->
                            first
            in
            go xs


{-| Cycle the timer through off / 1h / 2h / 4h / 8h (minutes).
-}
nextTimer : Int -> Int
nextTimer cur =
    case cur of
        0 ->
            60

        60 ->
            120

        120 ->
            240

        240 ->
            480

        _ ->
            0


update : Msg -> Model -> ( Model, Cmd Msg )
update msg model =
    case msg of
        Tick ->
            let
                i =
                    model.idle + 1
            in
            ( { model | idle = i, backlight = i < backlightTimeout, clock = model.clock + 1 }, Cmd.none )

        Event value ->
            applyEvent value model

        _ ->
            userUpdate msg { model | idle = 0, backlight = True }


userUpdate : Msg -> Model -> ( Model, Cmd Msg )
userUpdate msg model =
    let
        st =
            model.status
    in
    case msg of
        AddDevice ->
            ( { model | conn = Connecting, menuOpen = False, error = Nothing }
            , intent [ ( "kind", E.string "addDevice" ) ]
            )

        PickDevice id ->
            ( { model | conn = Connecting, menuOpen = False, error = Nothing }
            , intent [ ( "kind", E.string "connectId" ), ( "id", E.string id ) ]
            )

        RemoveDevice id ->
            -- The transport writes these before it disconnects, so the unit is
            -- back on the 0000 default and can be added again from any browser.
            ( { model | reveal = Nothing }
            , intent
                [ ( "kind", E.string "removeDevice" )
                , ( "id", E.string id )
                , ( "frames", encodeFrames (unlock defaultPin) )
                ]
            )

        RevealPin id ->
            ( { model | reveal = toggle model.reveal id }, Cmd.none )

        NewPasskey id ->
            ( { model | reveal = Just id }, Random.generate (PasskeyGenerated id) Codec.randomPin )

        PasskeyGenerated id pin ->
            ( model
            , intent
                [ ( "kind", E.string "setPasskey" )
                , ( "id", E.string id )
                , ( "pin", E.string pin )
                , ( "frames", encodeFrames (unlock pin) )
                ]
            )

        ToggleMenu ->
            ( { model | menuOpen = not model.menuOpen, reveal = Nothing }, Cmd.none )

        Disconnect ->
            ( { model | conn = Disconnected }, intent [ ( "kind", E.string "disconnect" ) ] )

        SetPower on ->
            ( { model | timerMin = 0, power = Power.command on model.clock model.power }
            , write True [ Codec.setPower on ]
            )

        SetTemp t ->
            ( model, write True [ Codec.setTemp t ] )

        CycleMode ->
            ( model, write True [ Codec.setMode (next Codec.modes st.mode) ] )

        CycleFan ->
            ( model, write True [ Codec.setFan (next Codec.fans st.fan) ] )

        SetSwing on ->
            -- Optimistic: the unit's swing read-back doesn't map cleanly to the
            -- axes, so track what we set rather than trusting decoded status.
            ( { model | swingV = on }, write True [ Codec.setSwing on ] )

        SetSwingH on ->
            ( { model | swingH = on }, write True [ Codec.setSwingH on ] )

        CycleTimer ->
            let
                m =
                    nextTimer model.timerMin

                timerFrame =
                    if model.power.on then
                        Codec.setOffTimer m

                    else
                        Codec.setOnTimer m
            in
            ( { model | timerMin = m }, write True [ timerFrame ] )

        _ ->
            ( model, Cmd.none )


toggle : Maybe String -> String -> Maybe String
toggle cur id =
    if cur == Just id then
        Nothing

    else
        Just id


applyEvent : D.Value -> Model -> ( Model, Cmd Msg )
applyEvent value model =
    case D.decodeValue eventDecoder value of
        Ok (StateEv s dev) ->
            ( { model | conn = s, device = orElse dev model.device, error = Nothing }, Cmd.none )

        Ok (NotifyEv bytes) ->
            -- A notification the codec will not vouch for is dropped, and with it
            -- any claim that the unit is talking to us.
            case Codec.decodeNotify bytes of
                Just dps ->
                    let
                        status =
                            Codec.toStatus dps model.status
                    in
                    ( { model
                        | status = status
                        , power = Power.report dps status model.clock model.power
                        , conn = Ready
                      }
                    , Cmd.none
                    )

                Nothing ->
                    ( model, Cmd.none )

        Ok (OpenedEv id pin isNew) ->
            ( model
            , Cmd.batch
                [ write False (unlock pin)

                -- A unit being paired for the first time is still on the 0000
                -- default; give it one of its own.
                , if isNew then
                    Random.generate (PasskeyGenerated id) Codec.randomPin

                  else
                    Cmd.none
                ]
            )

        Ok (DevicesEv devs current) ->
            ( { model
                | devices = devs
                , currentId = current
                , device =
                    case current |> Maybe.andThen (\cid -> devs |> List.filter (\d -> d.id == cid) |> List.head) of
                        Just d ->
                            Just d.name

                        Nothing ->
                            model.device
              }
            , Cmd.none
            )

        Ok (ErrorEv m) ->
            ( { model
                | error = Just m
                , conn =
                    if model.conn == Connecting then
                        Disconnected

                    else
                        model.conn
              }
            , Cmd.none
            )

        Err _ ->
            ( model, Cmd.none )


orElse : Maybe a -> Maybe a -> Maybe a
orElse a b =
    case a of
        Just _ ->
            a

        Nothing ->
            b


type Ev
    = StateEv Conn (Maybe String)
    | NotifyEv (List Int)
    | OpenedEv String String Bool
    | DevicesEv (List Dev) (Maybe String)
    | ErrorEv String


eventDecoder : D.Decoder Ev
eventDecoder =
    D.field "type" D.string
        |> D.andThen
            (\t ->
                case t of
                    "state" ->
                        D.map2 StateEv
                            (D.field "state" D.string |> D.map connFromString)
                            (D.maybe (D.field "device" D.string))

                    "notify" ->
                        D.map NotifyEv (D.field "bytes" (D.list D.int))

                    "opened" ->
                        D.map3 OpenedEv
                            (D.field "id" D.string)
                            (D.field "pin" D.string)
                            (D.oneOf [ D.field "isNew" D.bool, D.succeed False ])

                    "devices" ->
                        D.map2 DevicesEv
                            (D.field "devices" (D.list devDecoder))
                            (D.maybe (D.field "current" D.string))

                    "error" ->
                        D.map ErrorEv (D.field "message" D.string)

                    _ ->
                        D.fail ("unknown event " ++ t)
            )


defaultPin : String
defaultPin =
    "0000"


devDecoder : D.Decoder Dev
devDecoder =
    D.map4 Dev
        (D.field "id" D.string)
        (D.field "name" D.string)
        (D.oneOf [ D.field "pin" D.string, D.succeed defaultPin ])
        (D.oneOf [ D.field "available" D.bool, D.succeed True ])


connFromString : String -> Conn
connFromString s =
    case s of
        "connecting" ->
            Connecting

        "login" ->
            NeedPin

        "ready" ->
            Ready

        _ ->
            Disconnected



-- VIEW


view : Model -> Html Msg
view model =
    div [ class "stage" ]
        [ div [ class "remote", classList [ ( "asleep", model.conn /= Ready ) ] ]
            [ viewStatusBar model
            , viewLcd model.backlight model
            , viewError model.error
            , viewPad model
            , viewFooter
            ]
        ]


viewFooter : Html Msg
viewFooter =
    div [ class "source" ]
        [ a [ href "https://github.com/ananthb/hlmblue", target "_blank", rel "noopener" ]
            [ text "source" ]
        , span [ class "sep" ] [ text "·" ]
        , a [ href "https://github.com/ananthb/hlmblue/blob/main/LICENSE", target "_blank", rel "noopener" ]
            [ text "license" ]
        ]


viewError : Maybe String -> Html Msg
viewError err =
    case err of
        Just e ->
            div [ class "err" ] [ text e ]

        Nothing ->
            text ""



-- STATUS BAR: indicator LED + Bluetooth logo + device name + device menu.


viewStatusBar : Model -> Html Msg
viewStatusBar model =
    div [ class "statusbar" ]
        ([ button [ class "indicator", onClick ToggleMenu, title (tooltip model) ]
            [ span [ class ("led " ++ ledClass model.conn) ] []
            , img [ class "bt-logo", src btUri, alt "Bluetooth" ] []
            , span [ class "devname" ] [ text (Maybe.withDefault "Select A/C" model.device) ]
            , span [ class "caret" ] [ text "▾" ]
            ]
         , span [ class "brand" ] [ img [ class "brand-logo", src "logo-fins.svg", alt "" ] [], text "He A/C" ]
         ]
            ++ (if model.menuOpen then
                    [ div [ class "overlay", onClick ToggleMenu ] []
                    , viewMenu model
                    ]

                else
                    []
               )
        )


viewMenu : Model -> Html Msg
viewMenu model =
    div [ class "devmenu" ]
        (if List.isEmpty model.devices then
            [ div [ class "devmenu-empty" ] [ text "No saved A/Cs yet" ], addRow ]

         else
            List.map (viewDevRow model) model.devices ++ [ addRow ]
        )


addRow : Html Msg
addRow =
    button [ class "devmenu-add", onClick AddDevice ] [ text "+  Add an A/C" ]


viewDevRow : Model -> Dev -> Html Msg
viewDevRow model dev =
    let
        isCurrent =
            model.currentId == Just dev.id
    in
    div [ class "devrow-wrap" ]
        [ div [ classList [ ( "devrow", True ), ( "sel", isCurrent ) ] ]
            [ button [ class "devrow-pick", onClick (PickDevice dev.id) ]
                [ span
                    [ class
                        ("dot "
                            ++ (if dev.available then
                                    "ok"

                                else
                                    "off"
                               )
                        )
                    ]
                    []
                , span [ class "devrow-name" ] [ text dev.name ]
                ]
            , button [ class "devrow-x", title "Reset to 0000 and forget", onClick (RemoveDevice dev.id) ] [ text "×" ]
            ]
        , if isCurrent then
            viewPasskey model dev

          else
            text ""
        ]


viewPasskey : Model -> Dev -> Html Msg
viewPasskey model dev =
    let
        shown =
            model.reveal == Just dev.id
    in
    div [ class "pkrow" ]
        [ span [ class "pk-label" ] [ text "Passkey" ]
        , span [ class "pk-value" ]
            [ text
                (if shown then
                    dev.pin

                 else
                    "••••"
                )
            ]
        , button [ class "pk-btn", onClick (RevealPin dev.id) ]
            [ text
                (if shown then
                    "Hide"

                 else
                    "Show"
                )
            ]
        , button [ class "pk-btn", onClick (NewPasskey dev.id) ] [ text "Set new" ]
        ]


ledClass : Conn -> String
ledClass conn =
    case conn of
        Ready ->
            "btConnected"

        Connecting ->
            "btPairing"

        NeedPin ->
            "btPairing"

        Disconnected ->
            "btDisconnected"


tooltip : Model -> String
tooltip model =
    case model.conn of
        Ready ->
            "Connected to " ++ Maybe.withDefault "the A/C" model.device

        Connecting ->
            "Connecting…"

        NeedPin ->
            "Unlocking…"

        Disconnected ->
            "Disconnected — tap to pick or add an A/C"


btUri : String
btUri =
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23a9c1d6' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M8 8l8 8-4 4V4l4 4-8 8'/%3E%3C/svg%3E"



-- LCD


viewLcd : Bool -> Model -> Html Msg
viewLcd backlight model =
    div [ classList [ ( "lcd", True ), ( "dark", not backlight ) ] ] <|
        case model.conn of
            Ready ->
                viewReadout model.power.on model.swingV model.swingH model.status

            NeedPin ->
                placeholder "UNLOCKING"

            Connecting ->
                placeholder "SCANNING"

            Disconnected ->
                placeholder "OFFLINE"


viewReadout : Bool -> Bool -> Bool -> Codec.Status -> List (Html Msg)
viewReadout powerOn swingV swingH st =
    [ div [ class "lcd-top" ]
        [ span [ classList [ ( "seg", True ), ( "on", powerOn ) ] ] [ text (String.toUpper st.mode) ]
        , span [ class "room" ] [ text ("IN " ++ String.fromInt st.room ++ "°") ]
        ]
    , div [ class "lcd-main" ]
        [ span [ class "temp" ] [ text (String.fromInt st.temp) ]
        , span [ class "deg" ] [ text "°C" ]
        ]
    , div [ class "lcd-bot" ]
        [ span [ class "field" ] [ label_ "FAN", fanBars st.fan ]
        , span [ classList [ ( "field", True ), ( "on", swingV || swingH ) ] ] [ text (swingText swingV swingH) ]
        , span [ class "field watts" ] [ label_ "PWR", text (wattsText st) ]
        ]
    ]


placeholder : String -> List (Html Msg)
placeholder caption =
    [ div [ class "lcd-top" ] [ span [ class "seg" ] [ text "----" ], span [ class "room" ] [] ]
    , div [ class "lcd-main" ]
        [ span [ class "temp" ] [ text "--" ]
        , span [ class "deg" ] [ text "°C" ]
        ]
    , div [ class "lcd-bot" ] [ span [ class "field muted" ] [ text caption ] ]
    ]


wattsText : Codec.Status -> String
wattsText st =
    String.fromInt st.watts ++ " W"


swingText : Bool -> Bool -> String
swingText swingV swingH =
    case ( swingV, swingH ) of
        ( True, True ) ->
            "SWING ↕↔"

        ( True, False ) ->
            "SWING ↕"

        ( False, True ) ->
            "SWING ↔"

        ( False, False ) ->
            "SWING"


timerLabel : Int -> Bool -> String
timerLabel minutes power =
    if minutes <= 0 then
        "TIMER"

    else
        (if power then
            "OFF "

         else
            "ON "
        )
            ++ String.fromInt (minutes // 60)
            ++ "h"


label_ : String -> Html Msg
label_ s =
    span [ class "lbl" ] [ text s ]


fanBars : String -> Html Msg
fanBars fan =
    let
        lit =
            case fan of
                "low" ->
                    1

                "medium" ->
                    2

                "high" ->
                    3

                _ ->
                    0

        bar i =
            span [ classList [ ( "bar", True ), ( "on", i <= lit ) ] ] []
    in
    if fan == "auto" then
        span [ class "bars auto" ] [ text "AUTO" ]

    else
        span [ class "bars" ] (List.map bar [ 1, 2, 3 ])



-- BUTTON PAD


viewPad : Model -> Html Msg
viewPad model =
    case model.conn of
        Disconnected ->
            div [ class "pad connect" ]
                [ case model.devices of
                    d :: _ ->
                        button [ class "btn power big", onClick (PickDevice d.id) ] [ text "CONNECT" ]

                    [] ->
                        button [ class "btn power big", onClick AddDevice ] [ text "ADD AN A/C" ]
                ]

        Connecting ->
            div [ class "pad connect" ]
                [ button [ class "btn big", disabled True ] [ text "SCANNING…" ] ]

        NeedPin ->
            div [ class "pad connect" ]
                [ button [ class "btn big", disabled True ] [ text "UNLOCKING…" ] ]

        Ready ->
            let
                st =
                    model.status
            in
            div [ class "pad grid" ]
                [ button [ classList [ ( "btn", True ), ( "power", True ), ( "on", model.power.on ) ], onClick (SetPower (not model.power.on)) ]
                    [ glyph "⏻", small "POWER" ]
                , div [ class "rocker" ]
                    [ button [ class "btn up", onClick (SetTemp (Basics.min 30 (st.temp + 1))) ] [ text "+" ]
                    , span [ class "rocker-lbl" ] [ text "TEMP" ]
                    , button [ class "btn down", onClick (SetTemp (Basics.max 16 (st.temp - 1))) ] [ text "−" ]
                    ]
                , button [ class "btn", onClick CycleMode ] [ glyph (modeGlyph st.mode), small "MODE" ]
                , button [ class "btn", onClick CycleFan ] [ glyph "❋", small "FAN" ]
                , button [ classList [ ( "btn", True ), ( "on", model.swingV ) ], onClick (SetSwing (not model.swingV)) ] [ glyph "↕", small "SWING" ]
                , button [ classList [ ( "btn", True ), ( "on", model.swingH ) ], onClick (SetSwingH (not model.swingH)) ] [ glyph "↔", small "SWING H" ]
                , button [ classList [ ( "btn", True ), ( "on", model.timerMin > 0 ) ], onClick CycleTimer ] [ glyph "⏱", small (timerLabel model.timerMin model.power.on) ]
                , button [ class "btn ghost", onClick Disconnect ] [ glyph "⏏", small "EXIT" ]
                ]


glyph : String -> Html Msg
glyph g =
    span [ class "glyph" ] [ text g ]


small : String -> Html Msg
small s =
    span [ class "cap" ] [ text s ]


modeGlyph : String -> String
modeGlyph mode =
    case mode of
        "cool" ->
            "❄"

        "heat" ->
            "☀"

        "dry" ->
            "💧"

        "fan" ->
            "❋"

        _ ->
            "⟳"



-- MAIN


main : Program () Model Msg
main =
    Browser.element
        { init = init
        , update = update
        , view = view
        , subscriptions = subscriptions
        }


subscriptions : Model -> Sub Msg
subscriptions _ =
    Sub.batch
        [ bleEvents Event
        , Time.every 1000 (\_ -> Tick)
        ]
