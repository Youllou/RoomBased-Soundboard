import React, { useState, useEffect, useRef } from 'react';
import { Volume2, Users, LogIn, Copy, Check } from 'lucide-react';

export default function RoomSoundboard() {
    const [roomId, setRoomId] = useState('');
    const [currentRoom, setCurrentRoom] = useState(null);

    // Check URL for room code on mount
    useEffect(() => {
        const urlParams = new URLSearchParams(window.location.search);
        const roomFromUrl = urlParams.get('room');

        if (roomFromUrl) {
            setRoomId(roomFromUrl);
            joinRoom(roomFromUrl);
        }
    }, []);
    const [sounds, setSounds] = useState([]);
    const [userCount, setUserCount] = useState(0);
    const [copied, setCopied] = useState(false);
    const [connectionStatus, setConnectionStatus] = useState('disconnected');
    const wsRef = useRef(null);
    const audioRefs = useRef({});
    const audioBufferRef = useRef({});
    const reconnectTimeoutRef = useRef(null);

    // API URLs
    const API_URL = 'http://localhost:8000';
    const WS_URL = 'ws://localhost:8000';

    useEffect(() => {
        // Fetch sound metadata only (no audio files yet)
        fetch(`${API_URL}/api/sounds`)
            .then(res => res.json())
            .then(data => {
                setSounds(data.sounds);
            })
            .catch(err => console.error('Failed to fetch sounds:', err));

        return () => {
            if (wsRef.current) {
                wsRef.current.close();
            }
            if (reconnectTimeoutRef.current) {
                clearTimeout(reconnectTimeoutRef.current);
            }
        };
    }, []);

    const connectWebSocket = (room) => {
        if (wsRef.current) {
            wsRef.current.close();
        }

        setConnectionStatus('connecting');
        const ws = new WebSocket(`${WS_URL}/ws/${room}`);

        ws.onopen = () => {
            console.log('WebSocket connected');
            setConnectionStatus('connected');
        };

        ws.onmessage = (event) => {
            try {
                const message = JSON.parse(event.data);

                if (message.type === 'play_sound') {
                    // Load and play sound when received via websocket
                    loadAndPlaySound(message.soundId);
                } else if (message.type === 'user_count') {
                    setUserCount(message.count);
                }
            } catch (error) {
                console.error('Error parsing message:', error);
            }
        };

        ws.onerror = (error) => {
            console.error('WebSocket error:', error);
            setConnectionStatus('error');
        };

        ws.onclose = () => {
            console.log('WebSocket disconnected');
            setConnectionStatus('disconnected');

            if (currentRoom) {
                reconnectTimeoutRef.current = setTimeout(() => {
                    console.log('Attempting to reconnect...');
                    connectWebSocket(room);
                }, 3000);
            }
        };

        wsRef.current = ws;
    };

    const loadAndPlaySound = async (soundId) => {
        // Check if audio is already loaded
        if (audioBufferRef.current[soundId]) {
            playSound(soundId);
            return;
        }

        try {
            // Fetch the audio file from backend
            const response = await fetch(`${API_URL}/api/sounds/${soundId}/audio`);

            if (!response.ok) {
                console.error('Failed to load sound:', soundId);
                return;
            }

            const blob = await response.blob();
            const url = URL.createObjectURL(blob);

            // Create and cache audio element
            const audio = new Audio(url);
            audioRefs.current[soundId] = audio;
            audioBufferRef.current[soundId] = true;

            // Play the sound
            audio.play().catch(err => console.error('Audio play failed:', err));

        } catch (error) {
            console.error('Error loading sound:', error);
        }
    };

    const playSound = (soundId) => {
        const audio = audioRefs.current[soundId];
        if (audio) {
            audio.currentTime = 0;
            audio.play().catch(err => console.error('Audio play failed:', err));
        }
    };

    const joinRoom = (id) => {
        const room = id || roomId;
        if (!room) return;

        setCurrentRoom(room);

        // Update URL with room code
        const url = new URL(window.location);
        url.searchParams.set('room', room);
        window.history.pushState({}, '', url);

        connectWebSocket(room);
    };

    const leaveRoom = () => {
        if (reconnectTimeoutRef.current) {
            clearTimeout(reconnectTimeoutRef.current);
        }

        if (wsRef.current) {
            wsRef.current.close();
        }

        setCurrentRoom(null);
        setUserCount(0);
        setConnectionStatus('disconnected');

        // Remove room from URL
        const url = new URL(window.location);
        url.searchParams.delete('room');
        window.history.pushState({}, '', url);
    };

    const handlePlaySound = (soundId) => {
        // Load and play sound locally
        loadAndPlaySound(soundId);

        // Broadcast to other users in room
        if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
            wsRef.current.send(JSON.stringify({
                type: 'play_sound',
                soundId: soundId
            }));
        }
    };

    const copyRoomLink = () => {
        const url = new URL(window.location);
        url.searchParams.set('room', currentRoom);
        const fullUrl = url.toString();

        navigator.clipboard.writeText(fullUrl);
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
    };

    const generateRandomRoom = () => {
        const random = Math.random().toString(36).substring(2, 8);
        setRoomId(random);
        joinRoom(random);
    };

    const getConnectionStatusColor = () => {
        switch (connectionStatus) {
            case 'connected': return 'bg-green-500';
            case 'connecting': return 'bg-yellow-500';
            case 'error': return 'bg-red-500';
            default: return 'bg-gray-500';
        }
    };

    const getConnectionStatusText = () => {
        switch (connectionStatus) {
            case 'connected': return 'Connected';
            case 'connecting': return 'Connecting...';
            case 'error': return 'Connection Error';
            default: return 'Disconnected';
        }
    };

    if (!currentRoom) {
        return (
            <div className="min-h-screen bg-gradient-to-br from-purple-900 via-purple-800 to-indigo-900 flex items-center justify-center p-4">
                <div className="bg-white/10 backdrop-blur-lg rounded-2xl p-8 max-w-md w-full border border-white/20 shadow-2xl">
                    <div className="text-center mb-8">
                        <Volume2 className="w-16 h-16 mx-auto mb-4 text-purple-300" />
                        <h1 className="text-3xl font-bold text-white mb-2">Room Soundboard</h1>
                        <p className="text-purple-200">Create or join a room to share sounds in real-time</p>
                    </div>

                    <div className="space-y-4">
                        <div>
                            <input
                                type="text"
                                value={roomId}
                                onChange={(e) => setRoomId(e.target.value)}
                                placeholder="Enter room code"
                                className="w-full px-4 py-3 rounded-lg bg-white/20 border border-white/30 text-white placeholder-purple-200 focus:outline-none focus:ring-2 focus:ring-purple-400"
                                onKeyPress={(e) => e.key === 'Enter' && joinRoom()}
                            />
                        </div>

                        <button
                            onClick={() => joinRoom()}
                            disabled={!roomId}
                            className="w-full bg-purple-600 hover:bg-purple-700 disabled:bg-gray-600 disabled:cursor-not-allowed text-white font-semibold py-3 px-4 rounded-lg transition flex items-center justify-center gap-2"
                        >
                            <LogIn className="w-5 h-5" />
                            Join Room
                        </button>

                        <div className="relative">
                            <div className="absolute inset-0 flex items-center">
                                <div className="w-full border-t border-white/30"></div>
                            </div>
                            <div className="relative flex justify-center text-sm">
                                <span className="px-2 bg-transparent text-purple-200">or</span>
                            </div>
                        </div>

                        <button
                            onClick={generateRandomRoom}
                            className="w-full bg-indigo-600 hover:bg-indigo-700 text-white font-semibold py-3 px-4 rounded-lg transition"
                        >
                            Create Random Room
                        </button>
                    </div>
                </div>
            </div>
        );
    }

    return (
        <div className="min-h-screen bg-gradient-to-br from-purple-900 via-purple-800 to-indigo-900 p-4">
            <div className="max-w-4xl mx-auto">
                <div className="bg-white/10 backdrop-blur-lg rounded-2xl p-6 mb-6 border border-white/20">
                    <div className="flex items-center justify-between flex-wrap gap-4">
                        <div>
                            <h2 className="text-2xl font-bold text-white mb-1">Room: {currentRoom}</h2>
                            <div className="flex items-center gap-4 text-purple-200">
                                <div className="flex items-center gap-2">
                                    <Users className="w-4 h-4" />
                                    <span>{userCount} user{userCount !== 1 ? 's' : ''} in room</span>
                                </div>
                                <div className="flex items-center gap-2">
                                    <div className={`w-2 h-2 rounded-full ${getConnectionStatusColor()}`}></div>
                                    <span className="text-sm">{getConnectionStatusText()}</span>
                                </div>
                            </div>
                        </div>
                        <div className="flex gap-2">
                            <button
                                onClick={copyRoomLink}
                                className="bg-purple-600 hover:bg-purple-700 text-white font-semibold py-2 px-4 rounded-lg transition flex items-center gap-2"
                            >
                                {copied ? <Check className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
                                {copied ? 'Copied!' : 'Copy Link'}
                            </button>
                            <button
                                onClick={leaveRoom}
                                className="bg-red-600 hover:bg-red-700 text-white font-semibold py-2 px-4 rounded-lg transition"
                            >
                                Leave Room
                            </button>
                        </div>
                    </div>
                </div>

                <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
                    {sounds.map((sound) => (
                        <button
                            key={sound.id}
                            onClick={() => handlePlaySound(sound.id)}
                            disabled={connectionStatus !== 'connected'}
                            className="bg-white/10 backdrop-blur-lg hover:bg-white/20 border border-white/20 rounded-xl p-6 transition transform hover:scale-105 active:scale-95 disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                            <Volume2 className="w-8 h-8 mx-auto mb-2 text-purple-300" />
                            <p className="text-white font-semibold text-center">{sound.name}</p>
                        </button>
                    ))}
                </div>

                <div className="mt-6 bg-white/10 backdrop-blur-lg rounded-xl p-4 border border-white/20">
                    <p className="text-purple-200 text-sm text-center">
                        💡 Copy the link to share this room with friends - they'll join automatically!
                    </p>
                </div>
            </div>
        </div>
    );
}