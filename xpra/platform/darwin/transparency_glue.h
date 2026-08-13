/**
 * This file is part of Xpra.
 * Copyright (C) 2018 Antoine Martin <antoine@devloop.org.uk>
 */

//Just a wrapper for functions that are problematic to access with Cython

#import <Cocoa/Cocoa.h>

void setOpaque(NSWindow *window, BOOL opaque);
void setClearBackgroundColor(NSWindow *window);
void invalidateShadow(NSWindow *window);
void setHasShadow(NSWindow *window, BOOL hasShadow);
void orderWindowFront(NSWindow *window);

float getBackingScaleFactor(NSWindow *window);

void rememberButtonPressEvent(void);
void clearRememberedButtonPressEvent(void);
BOOL performWindowDragWithRememberedEvent(NSWindow *window, double maxAge);
